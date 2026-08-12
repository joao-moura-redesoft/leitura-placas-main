"""API de testes — gerencia dataset rotulado e executa avaliações de precisão."""
from __future__ import annotations
import json
import re
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File

router = APIRouter(prefix="/api/testes")

_DATASET = Path("testes/dataset.json")
_RESULTADOS = Path("testes/resultados")
_SNAPSHOTS = Path("app/web/static/snapshots")
_FOTOS_TESTE = Path("testes/fotos")

# Capturas que um humano olhou e recusou (ilegível, duplicada, sem placa). Sem esta
# lista elas voltariam para a fila de classificação a cada carga da tela, e a fila
# nunca chegaria ao fim — hoje são centenas de snapshots contra poucas dezenas úteis.
_DESCARTADOS = Path("testes/descartados.json")


def _ler_descartados() -> set[str]:
    if not _DESCARTADOS.exists():
        return set()
    return set(json.loads(_DESCARTADOS.read_text(encoding="utf-8")).get("arquivos", []))


def _salvar_descartados(arquivos: set[str]) -> None:
    _DESCARTADOS.parent.mkdir(parents=True, exist_ok=True)
    _DESCARTADOS.write_text(
        json.dumps({"arquivos": sorted(arquivos)}, indent=2, ensure_ascii=False),
        encoding="utf-8")


def _ler_dataset() -> dict:
    if not _DATASET.exists():
        return {"version": 1, "fotos": []}
    return json.loads(_DATASET.read_text(encoding="utf-8"))


def _salvar_dataset(ds: dict) -> None:
    _DATASET.parent.mkdir(parents=True, exist_ok=True)
    _DATASET.write_text(json.dumps(ds, indent=2, ensure_ascii=False), encoding="utf-8")


def _placa_do_nome(nome: str) -> str:
    """Extrai placa do padrão YYYYMMDDThhmmss_PLACA.jpg"""
    m = re.match(r"\d{8}T\d{6}_([A-Z0-9]{7})\.", nome.upper())
    return m.group(1) if m else ""


@router.get("/snapshots")
def listar_snapshots():
    arquivos = []

    def _add_dir(pasta: Path, url_prefix: str, arquivo_prefix: str):
        if not pasta.exists():
            return
        for f in sorted(pasta.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                continue
            if f.name.startswith("preview_"):
                tipo = "frame"
            else:
                tipo = "crop"
            arquivos.append({
                "nome": f.name,
                "arquivo": f"{arquivo_prefix}/{f.name}",
                "url": f"/{url_prefix}/{f.name}",
                "tipo": tipo,
                "placa_detectada": _placa_do_nome(f.name),
                "tamanho": f.stat().st_size,
                "data": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                "origem": "alpr" if pasta == _SNAPSHOTS else "upload",
            })

    _add_dir(_SNAPSHOTS, "static/snapshots", "static/snapshots")
    _add_dir(_FOTOS_TESTE, "testes/fotos", "testes/fotos")
    arquivos.sort(key=lambda x: x["data"], reverse=True)
    return arquivos


@router.post("/upload")
async def upload_foto(file: UploadFile = File(...)):
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "Apenas imagens são aceitas")
    ext = Path(file.filename or "foto.jpg").suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png"):
        raise HTTPException(400, "Formato não suportado (use JPG ou PNG)")
    _FOTOS_TESTE.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    nome = f"{ts}_{uuid.uuid4().hex[:6]}{ext}"
    dest = _FOTOS_TESTE / nome
    conteudo = await file.read()
    dest.write_bytes(conteudo)
    return {
        "ok": True,
        "nome": nome,
        "arquivo": f"testes/fotos/{nome}",
        "url": f"/testes/fotos/{nome}",
        "tamanho": len(conteudo),
    }


@router.get("/candidatos")
def listar_candidatos():
    """Fila de classificação: o que o ALPR capturou e ninguém rotulou nem recusou ainda.

    A placa que vem no nome do arquivo é O QUE O OCR LEU, não a verdade. Ela é devolvida
    como `placa_sugerida` (e nunca como `placa_correta`) porque aceitá-la em massa faria
    o dataset medir o OCR contra ele mesmo: a acurácia iria a ~100% sem significar nada.
    Quem classifica precisa conferir contra a imagem.
    """
    no_dataset = {f["arquivo"] for f in _ler_dataset()["fotos"]}
    descartados = _ler_descartados()
    fila = [
        s for s in listar_snapshots()
        if s["arquivo"] not in no_dataset and s["arquivo"] not in descartados
        # `preview_bico_N.jpg` é sobrescrito a cada leitura daquele bico. Rotular um
        # deles cria entrada de dataset cujo CONTEÚDO muda sozinho — foi assim que
        # `preview_4.jpg` virou uma linha apontando para arquivo inexistente.
        and not Path(s["arquivo"]).name.startswith("preview_")
    ]
    for s in fila:
        s["placa_sugerida"] = s.pop("placa_detectada", "")
    return {
        "candidatos": fila,
        "total": len(fila),
        "no_dataset": len(no_dataset),
        "descartados": len(descartados),
    }


@router.post("/descartar")
def descartar_candidato(payload: dict):
    """Tira uma captura da fila sem colocá-la no dataset."""
    arquivo = (payload.get("arquivo") or "").strip()
    if not arquivo:
        raise HTTPException(400, "arquivo obrigatório")
    d = _ler_descartados()
    d.add(arquivo)
    _salvar_descartados(d)
    return {"ok": True, "descartados": len(d)}


@router.delete("/descartar")
def restaurar_candidato(payload: dict):
    """Desfaz um descarte — devolve a captura para a fila."""
    arquivo = (payload.get("arquivo") or "").strip()
    d = _ler_descartados()
    if arquivo not in d:
        raise HTTPException(404, "Arquivo não está na lista de descartados")
    d.discard(arquivo)
    _salvar_descartados(d)
    return {"ok": True, "descartados": len(d)}


@router.get("/dataset")
def obter_dataset():
    return _ler_dataset()


@router.post("/dataset")
def adicionar_foto(payload: dict):
    arquivo = (payload.get("arquivo") or "").strip()
    placa = (payload.get("placa_correta") or "").upper().strip()
    if not arquivo or not placa:
        raise HTTPException(400, "arquivo e placa_correta são obrigatórios")

    ds = _ler_dataset()
    # Atualiza se já existe, senão insere
    # Origem (bico/posto) fica gravada para dar para saber de onde veio cada foto e,
    # depois, medir acurácia por posto — o dataset agora mistura vários clientes.
    origem = {k: payload[k] for k in ("bico_id", "origem") if payload.get(k)}

    # `layout` é campo próprio, e não texto em `obs`. Moto e carro são problemas
    # diferentes de OCR (a placa de moto é empilhada em duas linhas e chega com bem
    # menos pixels), mas `formato` só distingue mercosul/antigo — então o relatório
    # não conseguia mostrar a taxa de moto, que é justamente a que está em questão.
    layout = (payload.get("layout") or "").strip().lower()
    if layout and layout not in ("carro", "moto"):
        raise HTTPException(400, "layout deve ser 'carro' ou 'moto'")
    if layout:
        origem["layout"] = layout

    existente = next((f for f in ds["fotos"] if f["arquivo"] == arquivo), None)
    if existente:
        existente.update({
            "placa_correta": placa,
            "formato": payload.get("formato") or _inferir_formato(placa),
            "tipo": payload.get("tipo", "crop"),
            "obs": payload.get("obs", existente.get("obs", "")),
            **origem,
        })
    else:
        ds["fotos"].append({
            "id": uuid.uuid4().hex[:8],
            "arquivo": arquivo,
            "placa_correta": placa,
            "formato": payload.get("formato") or _inferir_formato(placa),
            "tipo": payload.get("tipo", "crop"),
            "obs": payload.get("obs", ""),
            **origem,
        })
    _salvar_dataset(ds)
    return {"ok": True, "total": len(ds["fotos"])}


@router.delete("/dataset")
def remover_foto(payload: dict):
    arquivo = (payload.get("arquivo") or "").strip()
    if not arquivo:
        raise HTTPException(400, "arquivo obrigatório")
    ds = _ler_dataset()
    antes = len(ds["fotos"])
    ds["fotos"] = [f for f in ds["fotos"] if f["arquivo"] != arquivo]
    if len(ds["fotos"]) == antes:
        raise HTTPException(404, "Foto não encontrada no dataset")
    _salvar_dataset(ds)
    return {"ok": True, "total": len(ds["fotos"])}


@router.post("/rodar")
def rodar_testes(payload: dict = {}):
    import sys
    from pathlib import Path as _Path
    _sys_path_backup = sys.path[:]
    sys.path.insert(0, str(_Path("testes").resolve().parent))
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("run_testes", "testes/run_testes.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        engines = payload.get("engines") or None
        salvar = bool(payload.get("salvar", False))
        if not engines:
            from app.core import config as cfg_mod
            cfg = cfg_mod.carregar()
            engines = [cfg.get("ocr_engine", "auto")]
        resultado = mod.rodar(engines=engines, salvar=salvar)
        return resultado
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        sys.path[:] = _sys_path_backup


@router.get("/bicos")
def listar_bicos():
    """Bicos cadastrados, com posto e câmera — alvo de captura do dataset.

    Capturar por BICO (e não por câmera) é o que faz o teste medir o mesmo que a
    produção: a leitura reativa analisa o recorte da área do bico, não o frame inteiro.
    """
    from app.core import banco
    empresas = {e["id"]: e for e in banco.empresas_listar()}
    automacoes = {a["id"]: a for a in banco.automacoes_listar()}
    cameras = {c["id"]: c for c in banco.cameras_listar()}
    saida = []
    for b in banco.bicos_listar():
        a = automacoes.get(b["automacao_id"])
        emp = empresas.get(a["empresa_id"]) if a else None
        cam = cameras.get(b["camera_id"])
        saida.append({
            "id": b["id"],
            "codigo": b["codigo"],
            "nome": b["nome"],
            "tem_roi": bool(b["roi"]),
            "camera_id": b["camera_id"],
            "camera_nome": cam["nome"] if cam else "?",
            "camera_local": cam.get("local", "") if cam else "",
            "automacao_codigo": a["codigo"] if a else "?",
            "posto": emp["nome"] if emp else "(sem posto)",
            "posto_id": emp["id"] if emp else None,
        })
    return saida


@router.post("/capturar-bico/{bico_id}")
def capturar_bico(bico_id: int, payload: dict = {}):
    """Captura da câmera do bico e salva JÁ RECORTADO pela área dele.

    É exatamente o que o detector recebe em produção — o dataset passa a medir o
    caminho real em vez do frame inteiro.
    """
    import cv2
    import json as _json
    from app.core import banco
    from app.core import config as cfg_mod
    from app.visao import camera as camera_mod
    from app.visao import leitura as leitura_mod

    bico = banco.bicos_obter(bico_id)
    if not bico:
        raise HTTPException(404, "Bico não encontrado")
    cam = banco.cameras_obter(bico["camera_id"])
    if not cam:
        raise HTTPException(404, "Câmera do bico não encontrada")

    auto = banco.automacoes_obter(bico["automacao_id"])
    emp = banco.empresas_obter(auto["empresa_id"]) if auto else None
    origem = f"{emp['nome'] if emp else '?'} / bico {bico['codigo']}"

    cfg = cfg_mod.carregar()
    with leitura_mod.lock_camera(cam["id"]):   # 1 conexão RTSP por câmera
        frame = camera_mod.capturar_frame_unico(
            tipo=cam["camera_tipo"],
            indice=cam.get("rtsp_url_custom") or cam.get("camera_indice", "0"),
            largura=int(cfg.get("camera_largura", "1280")),
            altura=int(cfg.get("camera_altura", "720")),
            fps=int(cfg.get("camera_fps", "15")),
            intelbras={
                "host": "" if cam.get("rtsp_url_custom") else cam.get("intelbras_host", ""),
                "porta": cam.get("intelbras_porta", "554"),
                "usuario": cam.get("intelbras_usuario", "admin"),
                "senha": cam.get("intelbras_senha") or cfg.get("intelbras_senha", ""),
                "canal": cam.get("intelbras_canal", "1"),
                "subtype": cam.get("intelbras_subtype", "1"),
                "formato": cam.get("intelbras_formato", "padrao"),
                "rtsp_transporte": cfg.get("rtsp_transporte", "tcp"),
            },
        )
    if frame is None:
        raise HTTPException(503, "Não foi possível capturar imagem da câmera")

    recortado = False
    if bico["roi"] and payload.get("aplicar_roi", True):
        r = _json.loads(bico["roi"]) if isinstance(bico["roi"], str) else bico["roi"]
        recorte = frame[r["y"]:r["y"] + r["h"], r["x"]:r["x"] + r["w"]]
        if recorte.size:
            frame, recortado = recorte, True

    _FOTOS_TESTE.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    # Nome com o CÓDIGO do bico (o que aparece na tela), não o id do banco — quem rotula
    # o dataset lê o nome do arquivo e id != código confunde.
    cod = re.sub(r"[^A-Za-z0-9_-]", "", bico["codigo"])[:12] or str(bico_id)
    nome = f"bico-{cod}_{ts}_{uuid.uuid4().hex[:4]}.jpg"
    dest = _FOTOS_TESTE / nome
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise HTTPException(500, "Falha ao codificar imagem")
    dest.write_bytes(buf.tobytes())

    return {
        "ok": True,
        "nome": nome,
        "arquivo": f"testes/fotos/{nome}",
        "url": f"/testes/fotos/{nome}",
        "tipo": "frame",           # é o que o detector recebe: área do bico, sem recorte de placa
        "recortado_por_roi": recortado,
        "sem_roi": not recortado,
        "largura": int(frame.shape[1]),
        "altura": int(frame.shape[0]),
        "bico_id": bico_id,
        "origem": origem,
        "tamanho": dest.stat().st_size,
    }


@router.get("/resultados")
def listar_resultados():
    _RESULTADOS.mkdir(parents=True, exist_ok=True)
    arquivos = sorted(_RESULTADOS.glob("*.json"), reverse=True)
    return [
        {"nome": f.name, "data": f.name[:15], "tamanho": f.stat().st_size}
        for f in arquivos
    ]


@router.get("/resultados/{nome}")
def obter_resultado(nome: str):
    if "/" in nome or "\\" in nome:
        raise HTTPException(400, "nome inválido")
    path = _RESULTADOS / nome
    if not path.exists():
        raise HTTPException(404, "Resultado não encontrado")
    return json.loads(path.read_text(encoding="utf-8"))


def _inferir_formato(placa: str) -> str:
    import re
    if re.match(r"^[A-Z]{3}[0-9][A-Z][0-9]{2}$", placa):
        return "mercosul"
    if re.match(r"^[A-Z]{3}[0-9]{4}$", placa):
        return "antigo"
    return "desconhecido"
