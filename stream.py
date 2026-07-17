"""Gerador MJPEG para streaming HTTP (multipart/x-mixed-replace)."""
from __future__ import annotations
import time

import cv2

import estado


def gerar_mjpeg(qualidade: int = 75, fps_max: int = 15):
    """Yield JPEG frames como multipart MJPEG."""
    intervalo = 1.0 / max(fps_max, 1)
    while True:
        inicio = time.time()
        frame = estado.obter_frame()
        if frame is not None:
            ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), qualidade])
            if ok:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n"
                    + jpg.tobytes()
                    + b"\r\n"
                )
        elapsed = time.time() - inicio
        if elapsed < intervalo:
            time.sleep(intervalo - elapsed)


def gerar_mjpeg_camera(camera_id: int, qualidade: int = 75, fps_max: int = 15):
    """Yield JPEG frames de uma câmera específica (por camera_db_id)."""
    intervalo = 1.0 / max(fps_max, 1)
    while True:
        inicio = time.time()
        frame = estado.obter_frame_camera(camera_id)
        if frame is not None:
            ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), qualidade])
            if ok:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n"
                    + jpg.tobytes()
                    + b"\r\n"
                )
        elapsed = time.time() - inicio
        if elapsed < intervalo:
            time.sleep(intervalo - elapsed)


def snapshot_jpeg(qualidade: int = 85) -> bytes | None:
    frame = estado.obter_frame()
    if frame is None:
        return None
    ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), qualidade])
    return jpg.tobytes() if ok else None
