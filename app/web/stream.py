"""Rotas de streaming: MJPEG ao vivo + snapshot atual."""
from __future__ import annotations
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse

from app.streaming import stream as stream_mod

router = APIRouter()


@router.get("/stream.mjpg")
def stream_mjpg():
    return StreamingResponse(
        stream_mod.gerar_mjpeg(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@router.get("/stream/{camera_id}.mjpg")
def stream_camera_mjpg(camera_id: int):
    return StreamingResponse(
        stream_mod.gerar_mjpeg_camera(camera_id),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@router.get("/snapshot.jpg")
def snapshot():
    jpg = stream_mod.snapshot_jpeg()
    if jpg is None:
        raise HTTPException(503, "Sem frame disponível")
    return Response(content=jpg, media_type="image/jpeg")
