"""Rotas de streaming: MJPEG ao vivo + snapshot atual."""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from app.core import banco
from app.streaming import stream as stream_mod
from app.web import deps

router = APIRouter()


@router.get("/stream.mjpg", dependencies=[Depends(deps.exigir_admin)])
def stream_mjpg():
    """Legado: último frame processado pelo processo, de QUALQUER câmera — não dá para
    escopar por posto (não se sabe de qual câmera é sem olhar). Admin-only por isso;
    o stream por câmera abaixo é o caminho correto (e escopado)."""
    return StreamingResponse(
        stream_mod.gerar_mjpeg(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@router.get("/stream/{camera_id}.mjpg")
def stream_camera_mjpg(camera_id: int, request: Request):
    cam = banco.cameras_obter(camera_id)
    if cam:
        deps.checar_acesso_empresa(request, cam.get("empresa_id"))
    return StreamingResponse(
        stream_mod.gerar_mjpeg_camera(camera_id),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@router.get("/snapshot.jpg", dependencies=[Depends(deps.exigir_admin)])
def snapshot():
    """Legado — mesmo motivo de `/stream.mjpg` acima."""
    jpg = stream_mod.snapshot_jpeg()
    if jpg is None:
        raise HTTPException(503, "Sem frame disponível")
    return Response(content=jpg, media_type="image/jpeg")
