#!/usr/bin/env python3
"""
Roda o pipeline de OCR em todas as fotos do dataset e avalia a precisão.

Cada engine roda num SUBPROCESSO próprio. O motivo é memória, não organização: a pilha
de um engine é grande (easyocr carrega PyTorch, paddleocr carrega Paddle, o detector de
leitura abre 3 sessões ONNX) e carregar mais de uma na mesma memória estoura em máquina
de desenvolvimento. Quando estourava, o processo morria sem mensagem útil — só
"Segmentation fault", ou um `OpenBLAS error` enganoso que parecia conflito de biblioteca
e era falta de RAM ("DefaultCPUAllocator: not enough memory" ao pedir 9 MB). Isolando,
o pico de memória é o de UM engine, e um engine que não couber vira uma linha de falha
no relatório em vez de derrubar a medição inteira.

Isso também protege o servidor: `/api/testes/rodar` chama `rodar()` dentro do processo
do FastAPI, que já tem pipeline e modelos carregados.

Uso:
  python testes/run_testes.py
  python testes/run_testes.py --engine fast_plate_ocr --engine easyocr
  python testes/run_testes.py --comparar
  python testes/run_testes.py --salvar
  python testes/run_testes.py --caminho live
  python testes/run_testes.py --em-processo      # sem subprocesso (depuração)
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)

import cv2
from app.core import estado


class _DetectorPreguicoso:
    """Só carrega o detector se alguma foto do dataset for `tipo: frame`.

    O dataset é quase todo `crop` (placa já recortada) e vai direto pro OCR — hoje 1 foto
    em 42 usa detector. Carregá-lo sempre custava 3 sessões ONNX (placa, janelas, veículo)
    de memória que, somadas ao engine de OCR, são justamente o que faz a medição estourar
    numa máquina apertada.
    """

    def __init__(self, cfg: dict, caminho: str):
        self._cfg, self._caminho, self._det = cfg, caminho, None

    def detectar(self, img):
        if self._det is None:
            self._det = _criar_detector(self._cfg, self._caminho)
        return self._det.detectar(img)


def _criar_detector(cfg: dict, caminho: str = "leitura"):
    """Cria o MESMO detector que roda em produção — nunca um montado à mão aqui.

    caminho="leitura" → botão "Ler Placa"/GET (obter_detector_leitura): modelo
                        dedicado + 2 estágios veículo→placa. É o caminho que o
                        bico aciona, então é o padrão dos testes.
    caminho="live"    → pipeline do stream ao vivo (criar_detector).

    Antes esta função montava um `Detector` ONNX à mão apontando para
    `modelo_path`. Com o backend padrão (`detector_backend=open_image_models`)
    esse arquivo não existe, então o detector caía silenciosamente no fallback
    por contornos — os testes mediam um detector que não existe em produção, e
    a acurácia relatada não dizia nada sobre o sistema real.
    """
    from app.visao.detector import criar_detector, obter_detector_leitura
    if caminho == "leitura":
        return obter_detector_leitura(cfg)   # já retorna carregado
    det = criar_detector(cfg)
    det.carregar()
    return det


def _criar_ocr(cfg: dict, caminho: str = "leitura"):
    """Cria o MESMO OCR que roda em produção — mesmo motivo do `_criar_detector`.

    caminho="leitura" → `obter_ocr_leitura`: com `ocr_engine=auto` e
                        `ocr_leitura_paddle=sim` (o padrão) isso é o ensemble
                        `AutoOCRPaddle`, que é quem o botão "Ler Placa" usa.
    caminho="live"    → a mesma fábrica com o reforço do Paddle desligado, que
                        constrói exatamente o que o `Pipeline` monta no seu
                        __init__ (AutoOCR / MultiOCR / OCR, com os parâmetros de
                        deskew). Se o pipeline mudar de OCR, esta equivalência
                        precisa ser revista — ela é por construção, não checada.

    Antes esta função montava um `AutoOCR` à mão. Ele é a CLASSE-PAI do que roda
    na leitura GET: sem o reforço do PaddleOCR e sem os parâmetros de deskew do
    config. O harness media um leitor mais fraco que o de produção — e, em placa
    de moto, media justamente o caminho onde o Paddle é a diferença entre 2/27 e
    22/27.
    """
    from app.visao.ocr import obter_ocr_leitura
    if caminho != "leitura":
        cfg = {**cfg, "ocr_leitura_paddle": "nao"}
    return obter_ocr_leitura(cfg)          # já retorna carregado


def _testar_foto(foto: dict, det, ocr, cfg: dict, crops_dir: Path | None = None) -> dict:
    from app.visao.validador import validar
    from app.visao.pipeline import _expandir_bbox

    arquivo = str(_ROOT / foto["arquivo"])
    img = cv2.imread(arquivo)
    placa_correta = foto["placa_correta"].upper().strip()
    tipo = foto.get("tipo", "crop")

    if img is None:
        return {"status": "erro", "motivo": "arquivo não encontrado",
                "lido": "", "esperado": placa_correta, **foto}

    conf_ocr = 0.0

    if tipo == "frame":
        bboxes = det.detectar(img)
        if not bboxes:
            return {"status": "falhou", "motivo": "YOLO sem detecção",
                    "lido": "", "esperado": placa_correta, **foto}
        f_h, f_w = img.shape[:2]
        candidatos = []
        for x, y, w, h, conf_det in bboxes:
            x, y, w, h = _expandir_bbox(x, y, w, h, f_w, f_h)
            crop = img[y:y+h, x:x+w]
            if crop.size == 0:
                continue
            texto, c_ocr = ocr.ler(crop)
            resultado = validar(texto)
            with estado.lock:
                crop_bytes = estado.ultimo_crop_ocr_jpg
            candidatos.append((resultado[0] if resultado else "", conf_det, c_ocr, crop_bytes))
        match = next((c for c in candidatos if c[0] == placa_correta), None)
        if match:
            crop_url = _salvar_crop_processado(foto, crops_dir, match[3])
            return {"status": "ok", "lido": match[0], "esperado": placa_correta,
                    "conf_ocr": round(match[2], 3), "crop_processado": crop_url, **foto}
        
        melhor = max(candidatos, key=lambda c: c[1]) if candidatos else ("", 0, 0, None)
        lido, conf_ocr, melhor_bytes = melhor[0], melhor[2], melhor[3] if len(melhor) == 4 else None
        crop_url = _salvar_crop_processado(foto, crops_dir, melhor_bytes)
    else:
        texto, conf_ocr = ocr.ler(img)
        resultado = validar(texto)
        lido = resultado[0] if resultado else ""
        crop_url = _salvar_crop_processado(foto, crops_dir)
    correto = (lido == placa_correta)
    return {
        "status": "ok" if correto else "errou",
        "lido": lido,
        "esperado": placa_correta,
        "conf_ocr": round(conf_ocr, 3),
        "crop_processado": crop_url,
        **foto,
    }


def _char_diff(esperado: str, lido: str) -> list[tuple[str, str]]:
    diffs = []
    for e, l in zip(esperado.ljust(7)[:7], lido.ljust(7)[:7]):
        if e != l:
            diffs.append((e, l))
    return diffs


def _salvar_crop_processado(foto: dict, crops_dir: Path | None, jpg_bytes: bytes = None) -> str | None:
    """Salva o último crop pós-processado pelo OCR (deskew+perspectiva+header).

    Captura de estado.ultimo_crop_ocr_jpg que é atualizado pelo OCR.ler().
    Retorna URL relativa ou None se não há crop disponível.
    """
    if crops_dir is None:
        return None
    if not jpg_bytes:
        with estado.lock:
            jpg_bytes = estado.ultimo_crop_ocr_jpg
    if not jpg_bytes:
        return None
    nome_base = Path(foto.get("arquivo", "desconhecido")).stem
    nome = f"{nome_base}_proc.jpg"
    dest = crops_dir / nome
    dest.write_bytes(jpg_bytes)
    return f"/testes/resultados/crops/{nome}"


def _carregar_fotos() -> list[dict]:
    dataset_path = Path(__file__).parent / "dataset.json"
    if not dataset_path.exists():
        print("Dataset não encontrado: testes/dataset.json")
        return []
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    fotos = dataset.get("fotos", [])
    if not fotos:
        print("Dataset vazio — adicione fotos em /testes na interface web.")
    return fotos


def _rodar_engine(engine_name: str, fotos: list[dict], caminho: str) -> dict:
    """Mede UM engine no dataset inteiro, no processo atual.

    É aqui que os modelos são carregados, então é este o corpo que roda dentro do
    subprocesso — quem chama é `_rodar_engine_isolado`.
    """
    print(f"\n{'='*62}")
    print(f"  ENGINE: {engine_name.upper()}")
    print(f"{'='*62}")

    from app.core import config as cfg_mod
    cfg = cfg_mod.carregar()
    cfg["ocr_engine"] = engine_name

    det = _DetectorPreguicoso(cfg, caminho)
    ocr = _criar_ocr(cfg, caminho)

    # Diretório para crops pós-processados (visível no frontend)
    crops_dir = Path(__file__).parent / "resultados" / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)

    resultados = []
    for foto in fotos:
        r = _testar_foto(foto, det, ocr, cfg, crops_dir=crops_dir)
        resultados.append(r)

    total = len(resultados)
    ok    = sum(1 for r in resultados if r["status"] == "ok")
    erros  = [r for r in resultados if r["status"] == "errou"]
    falhas = [r for r in resultados if r["status"] == "falhou"]
    nf     = [r for r in resultados if r["status"] == "erro"]

    print(f"\n  ACURACIA: {ok}/{total}  ({ok/total*100:.1f}%)")
    if falhas:
        print(f"  Sem detecção YOLO: {len(falhas)}")
    if nf:
        print(f"  Arquivo não encontrado: {len(nf)}")

    confusoes: dict[tuple, int] = defaultdict(int)
    for r in erros:
        for esp, lid in _char_diff(r["esperado"], r.get("lido", "")):
            confusoes[(esp, lid)] += 1

    if erros:
        print(f"\n  ERROS ({len(erros)}):")
        for r in erros:
            diffs = " ".join(f"{e}->{l}" for e, l in _char_diff(r["esperado"], r.get("lido", "")))
            arq = Path(r.get("arquivo", "")).name
            print(f"    {arq:<35}  esperado={r['esperado']}  lido={r.get('lido') or '(nada)':>8}  [{diffs}]")

    if confusoes:
        print(f"\n  CONFUSOES DE CARACTERES:")
        for (e, l), n in sorted(confusoes.items(), key=lambda x: -x[1]):
            print(f"    {e} -> {l}  ({n}x)")

    formatos: dict = defaultdict(lambda: {"ok": 0, "total": 0})
    for r in resultados:
        fmt = r.get("formato", "?")
        formatos[fmt]["total"] += 1
        if r["status"] == "ok":
            formatos[fmt]["ok"] += 1
    print(f"\n  POR FORMATO:")
    for fmt, st in sorted(formatos.items()):
        pct = st["ok"] / st["total"] * 100 if st["total"] else 0
        print(f"    {fmt:<12}  {st['ok']}/{st['total']}  ({pct:.1f}%)")

    # Moto é o problema em aberto, e `formato` (mercosul/antigo) não o separa: a placa de
    # moto é empilhada em duas linhas e chega com bem menos pixels. Fotos rotuladas antes
    # do campo `layout` existir aparecem como "?" — é ruído honesto, não zero.
    layouts: dict = defaultdict(lambda: {"ok": 0, "total": 0})
    for r in resultados:
        layouts[r.get("layout") or "?"]["total"] += 1
        if r["status"] == "ok":
            layouts[r.get("layout") or "?"]["ok"] += 1
    print(f"\n  POR LAYOUT:")
    for lay, st in sorted(layouts.items()):
        pct = st["ok"] / st["total"] * 100 if st["total"] else 0
        print(f"    {lay:<12}  {st['ok']}/{st['total']}  ({pct:.1f}%)")

    return {
        "acuracia": round(ok / total, 4) if total else 0,
        "ok": ok, "erros": len(erros), "falhas_deteccao": len(falhas), "total": total,
        "por_formato": {fmt: {"ok": s["ok"], "total": s["total"]} for fmt, s in formatos.items()},
        "por_layout": {lay: {"ok": s["ok"], "total": s["total"]} for lay, s in layouts.items()},
        "confusoes": {f"{e}>{l}": n for (e, l), n in confusoes.items()},
        "detalhes": resultados,
    }


def _engine_falhou(engine_name: str, total_fotos: int, returncode: int, houve_saida: bool) -> dict:
    """Linha de falha no relatório, no lugar de derrubar a medição inteira."""
    if houve_saida:
        motivo = f"o subprocesso gravou resultado ilegível (código {returncode})"
    elif returncode == 0:
        motivo = "o subprocesso terminou sem gravar resultado"
    else:
        # Morte por sinal: negativo no POSIX, 0xC0000005/0xC0000409 no Windows.
        morreu = returncode < 0 or returncode > 255
        causa = " — quase sempre falta de memória ao carregar o engine" if morreu else ""
        motivo = f"o subprocesso terminou com código {returncode}{causa}"

    print(f"\n  !! {engine_name}: {motivo}")
    print(f"     Os outros engines continuam. Para ver o erro inteiro, sem isolamento:")
    print(f"     python testes/run_testes.py --engine {engine_name} --em-processo")
    return {
        "erro": motivo,
        "acuracia": 0, "ok": 0, "erros": 0, "falhas_deteccao": 0, "total": total_fotos,
        "por_formato": {}, "confusoes": {}, "detalhes": [],
    }


def _rodar_engine_isolado(engine_name: str, fotos: list[dict], caminho: str) -> dict:
    """Roda um engine em subprocesso e traz o resultado de volta por arquivo.

    O resultado volta por arquivo temporário, e não pelo stdout, de propósito: assim o
    stdout do filho é herdado e a medição aparece ao vivo no terminal, como antes.
    """
    fd, saida = tempfile.mkstemp(prefix=f"run_testes_{engine_name}_", suffix=".json")
    os.close(fd)
    cmd = [
        sys.executable, str(Path(__file__).resolve()),
        "--engine", engine_name,
        "--caminho", caminho,
        "--em-processo",              # o filho mede de verdade; sem isso, forkaria de novo
        "--saida-worker", saida,
    ]
    # O filho herda um stdout que pode não ser um console (servidor com log em arquivo);
    # sem isto, um acento no relatório viraria UnicodeEncodeError e perderia a medição.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        proc = subprocess.run(cmd, cwd=str(_ROOT), env=env)
        texto = Path(saida).read_text(encoding="utf-8").strip()
        if proc.returncode == 0 and texto:
            try:
                return json.loads(texto)
            except json.JSONDecodeError:
                return _engine_falhou(engine_name, len(fotos), proc.returncode, True)
        return _engine_falhou(engine_name, len(fotos), proc.returncode, bool(texto))
    except OSError as e:
        print(f"\n  !! {engine_name}: não foi possível lançar o subprocesso ({e})")
        return _engine_falhou(engine_name, len(fotos), -1, False)
    finally:
        try:
            os.unlink(saida)
        except OSError:
            pass


def rodar(engines: list[str], salvar: bool = False, caminho: str = "leitura",
          em_processo: bool = False) -> dict:
    fotos = _carregar_fotos()
    if not fotos:
        return {}

    relatorio = {
        "data": datetime.now().isoformat(),
        "total_fotos": len(fotos),
        "engines": {},
    }

    for engine_name in engines:
        if em_processo:
            relatorio["engines"][engine_name] = _rodar_engine(engine_name, fotos, caminho)
        else:
            relatorio["engines"][engine_name] = _rodar_engine_isolado(engine_name, fotos, caminho)

    print(f"\n{'='*62}\n  RESUMO COMPARATIVO")
    print(f"{'='*62}")
    for eng, stats in relatorio["engines"].items():
        if stats.get("erro"):
            print(f"  {eng:<20}  não mediu ({stats['erro']})")
            continue
        ok, total = stats.get("ok", 0), stats.get("total", 0)
        print(f"  {eng:<20}  {ok}/{total}  ({stats.get('acuracia', 0)*100:.1f}%)")

    if salvar:
        out_dir = Path(__file__).parent / "resultados"
        out_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        out_path = out_dir / f"{ts}.json"
        out_path.write_text(json.dumps(relatorio, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n  Relatório salvo: {out_path}")

    return relatorio


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Avalia precisão do OCR no dataset rotulado")
    parser.add_argument("--engine", action="append", dest="engines",
                        metavar="ENGINE",
                        help="Engine a testar (pode repetir). Padrão: usa config.txt")
    parser.add_argument("--comparar", action="store_true",
                        help="Compara auto, fast_plate_ocr, easyocr e tesseract")
    parser.add_argument("--salvar", action="store_true",
                        help="Salva resultado JSON em testes/resultados/")
    parser.add_argument("--caminho", choices=["leitura", "live"], default="leitura",
                        help="Qual detector de produção testar: 'leitura' = botão "
                             "Ler Placa/GET (padrão), 'live' = stream ao vivo")
    parser.add_argument("--em-processo", action="store_true", dest="em_processo",
                        help="Carrega os engines neste processo, sem isolar. Só para "
                             "depurar: é assim que se vê o erro que o subprocesso engole")
    # Uso interno: é o que o pai passa ao filho para receber o resultado de volta.
    parser.add_argument("--saida-worker", dest="saida_worker", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.comparar:
        engines_run = ["auto", "fast_plate_ocr", "easyocr", "tesseract"]
    elif args.engines:
        engines_run = args.engines
    else:
        from app.core import config as cfg_mod
        cfg = cfg_mod.carregar()
        engines_run = [cfg.get("ocr_engine", "auto")]

    if args.saida_worker:
        # Filho: mede UM engine e devolve o bloco do relatório. Sem resumo e sem salvar —
        # quem junta e grava é o pai.
        fotos_worker = _carregar_fotos()
        stats = _rodar_engine(engines_run[0], fotos_worker, args.caminho) if fotos_worker else {}
        Path(args.saida_worker).write_text(
            json.dumps(stats, ensure_ascii=False), encoding="utf-8")
        sys.exit(0)

    rodar(engines=engines_run, salvar=args.salvar, caminho=args.caminho,
          em_processo=args.em_processo)
