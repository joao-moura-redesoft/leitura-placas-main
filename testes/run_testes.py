#!/usr/bin/env python3
"""
Roda o pipeline de OCR em todas as fotos do dataset e avalia a precisão.

Uso:
  python testes/run_testes.py
  python testes/run_testes.py --engine fast_plate_ocr --engine easyocr
  python testes/run_testes.py --comparar
  python testes/run_testes.py --salvar
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)

import cv2
from app.core import estado


def _criar_detector(cfg: dict):
    from app.visao.detector import Detector, MultiDetector
    conf = float(cfg["conf_threshold"])
    nms  = float(cfg["nms_threshold"])
    extras = [e.strip() for e in cfg.get("detector_modelos_extra", "").split(",") if e.strip()]
    if extras:
        from app.visao.detector import MultiDetector
        dets = [Detector(cfg["modelo_path"], conf, nms)]
        for m in extras:
            dets.append(Detector(m, conf, nms))
        det = MultiDetector(dets, votos_minimos=max(1, int(cfg.get("detector_votos_minimos", "1"))))
    else:
        det = Detector(cfg["modelo_path"], conf, nms)
    det.carregar()
    return det


def _criar_ocr(cfg: dict):
    from app.visao.ocr import OCR, AutoOCR, MultiOCR
    engine = cfg.get("ocr_engine", "tesseract")
    psm = int(cfg.get("tesseract_psm", "7"))
    extras = [e.strip() for e in cfg.get("ocr_engines_extra", "").split(",") if e.strip()]
    if engine == "auto":
        ocr = AutoOCR(tesseract_psm=psm)
    elif extras:
        ocr = MultiOCR(engines=[engine] + extras, tesseract_psm=psm)
    else:
        ocr = OCR(engine=engine, tesseract_psm=psm)
    ocr.carregar()
    return ocr


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


def rodar(engines: list[str], salvar: bool = False) -> dict:
    dataset_path = Path(__file__).parent / "dataset.json"
    if not dataset_path.exists():
        print("Dataset não encontrado: testes/dataset.json")
        return {}

    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    fotos = dataset.get("fotos", [])
    if not fotos:
        print("Dataset vazio — adicione fotos em /testes na interface web.")
        return {}

    relatorio = {
        "data": datetime.now().isoformat(),
        "total_fotos": len(fotos),
        "engines": {},
    }

    for engine_name in engines:
        print(f"\n{'='*62}")
        print(f"  ENGINE: {engine_name.upper()}")
        print(f"{'='*62}")

        from app.core import config as cfg_mod
        cfg = cfg_mod.carregar()
        cfg["ocr_engine"] = engine_name

        det = _criar_detector(cfg)
        ocr = _criar_ocr(cfg)

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

        relatorio["engines"][engine_name] = {
            "acuracia": round(ok / total, 4) if total else 0,
            "ok": ok, "erros": len(erros), "falhas_deteccao": len(falhas), "total": total,
            "por_formato": {fmt: {"ok": s["ok"], "total": s["total"]} for fmt, s in formatos.items()},
            "confusoes": {f"{e}>{l}": n for (e, l), n in confusoes.items()},
            "detalhes": resultados,
        }

    print(f"\n{'='*62}\n  RESUMO COMPARATIVO")
    print(f"{'='*62}")
    for eng, stats in relatorio["engines"].items():
        print(f"  {eng:<20}  {stats['ok']}/{stats['total']}  ({stats['acuracia']*100:.1f}%)")

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
    args = parser.parse_args()

    if args.comparar:
        engines_run = ["auto", "fast_plate_ocr", "easyocr", "tesseract"]
    elif args.engines:
        engines_run = args.engines
    else:
        from app.core import config as cfg_mod
        cfg = cfg_mod.carregar()
        engines_run = [cfg.get("ocr_engine", "auto")]

    rodar(engines=engines_run, salvar=args.salvar)
