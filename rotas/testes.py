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
_SNAPSHOTS = Path("static/snapshots")
_FOTOS_TESTE = Path("testes/fotos")


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
    existente = next((f for f in ds["fotos"] if f["arquivo"] == arquivo), None)
    if existente:
        existente.update({
            "placa_correta": placa,
            "formato": payload.get("formato") or _inferir_formato(placa),
            "tipo": payload.get("tipo", "crop"),
            "obs": payload.get("obs", existente.get("obs", "")),
        })
    else:
        ds["fotos"].append({
            "id": uuid.uuid4().hex[:8],
            "arquivo": arquivo,
            "placa_correta": placa,
            "formato": payload.get("formato") or _inferir_formato(placa),
            "tipo": payload.get("tipo", "crop"),
            "obs": payload.get("obs", ""),
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
            import config as cfg_mod
            cfg = cfg_mod.carregar()
            engines = [cfg.get("ocr_engine", "auto")]
        resultado = mod.rodar(engines=engines, salvar=salvar)
        return resultado
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        sys.path[:] = _sys_path_backup


@router.get("/cameras")
def listar_cameras():
    """Retorna câmeras cadastradas para seleção no capturador."""
    import banco
    cameras = banco.cameras_listar()
    return [
        {"id": c["id"], "nome": c["nome"], "bomba": c["bomba"], "lado": c["lado"]}
        for c in cameras if c.get("ativo", 1)
    ]


@router.post("/capturar-camera/{camera_id}")
def capturar_camera(camera_id: int, payload: dict = {}):
    """Captura um frame da câmera, salva em testes/fotos/ e retorna info do arquivo."""
    import cv2
    import time as _time
    import banco
    import estado
    import pipeline
    import camera as camera_mod
    import config as cfg_mod

    cam = banco.cameras_obter(camera_id)
    if not cam:
        raise HTTPException(404, "Câmera não encontrada")

    frame = None

    # Reusa frame do pipeline se estiver rodando
    if camera_id in pipeline._instancias:
        for _ in range(80):
            frame = estado.obter_frame_camera(camera_id)
            if frame is not None:
                break
            _time.sleep(0.1)

    if frame is None:
        cfg = cfg_mod.carregar()
        intelbras = {
            "host": cam["intelbras_host"],
            "porta": cam["intelbras_porta"],
            "usuario": cam["intelbras_usuario"],
            "senha": cam["intelbras_senha"] or cfg.get("intelbras_senha", ""),
            "canal": cam["intelbras_canal"],
            "subtype": cam["intelbras_subtype"],
            "formato": cam["intelbras_formato"],
        }
        ok, msg, jpg_bytes = camera_mod.capturar_teste(
            tipo=cam["camera_tipo"],
            indice=cam["camera_indice"],
            largura=1280, altura=720, fps=15,
            intelbras=intelbras,
        )
        if not ok or jpg_bytes is None:
            raise HTTPException(503, msg)
        # Decodifica para numpy para salvar uniformemente
        import numpy as np
        arr = np.frombuffer(jpg_bytes, np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)

    if frame is None:
        raise HTTPException(503, "Não foi possível capturar frame da câmera")

    _FOTOS_TESTE.mkdir(parents=True, exist_ok=True)
    tipo_img = payload.get("tipo", "frame")
    prefixo = "preview_" if tipo_img == "frame" else ""
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    nome = f"{prefixo}{ts}_cam{camera_id}_{uuid.uuid4().hex[:4]}.jpg"
    dest = _FOTOS_TESTE / nome
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise HTTPException(500, "Falha ao codificar imagem")
    dest.write_bytes(buf.tobytes())

    return {
        "ok": True,
        "nome": nome,
        "arquivo": f"testes/fotos/{nome}",
        "url": f"/testes/fotos/{nome}",
        "tipo": tipo_img,
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
