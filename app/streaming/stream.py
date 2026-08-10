"""Gerador MJPEG para streaming HTTP (multipart/x-mixed-replace)."""
from __future__ import annotations
import threading
import time

import cv2

from app.core import estado

# ── Cache de JPEG por câmera ─────────────────────────────────────────────────
# O pipeline publica frame novo a `deteccao_fps_max` (ex.: 5/s), mas cada gerador
# MJPEG roda a `fps_max` (15/s) — sem cache, cada VIEWER reencoda o MESMO frame
# várias vezes (~30ms/encode em 1280x720 nesta máquina). Com N viewers na mesma
# câmera, o custo de encode escalava com N por nada: o frame não tinha mudado.
#
# Identidade, não conteúdo: comparar `is` em vez de comparar pixels é o que torna
# o cache barato de consultar. É seguro porque o pipeline nunca desenha por cima de
# um frame já publicado — as bboxes vão numa cópia ANTES de `estado.registrar_frame*`
# (ver app/visao/pipeline.py). Se algum dia alguém mutar um frame publicado in-place,
# este cache passaria a servir bytes desatualizados — não faça isso.
_cache_lock = threading.Lock()
_cache_jpeg: dict[object, tuple[object, int, bytes]] = {}   # chave -> (frame, qualidade, jpg)


def _jpeg_cacheado(chave, frame, qualidade: int) -> bytes | None:
    """JPEG do frame, reusando o último encode quando é o MESMO objeto de frame e a
    mesma qualidade. `chave` tipicamente é o camera_db_id (ou "global")."""
    with _cache_lock:
        item = _cache_jpeg.get(chave)
        if item is not None and item[0] is frame and item[1] == qualidade:
            return item[2]
    # imencode FORA do lock (~30ms): segurar o lock aqui serializaria todos os
    # viewers dessa câmera. No pior caso dois viewers encodam o mesmo frame uma vez
    # cada — nunca produz bytes errados, só um encode redundante ocasional.
    ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), qualidade])
    if not ok:
        return None
    dados = jpg.tobytes()
    with _cache_lock:
        _cache_jpeg[chave] = (frame, qualidade, dados)
    return dados


def descartar_cache(chave) -> None:
    """Remove a entrada de uma câmera do cache. Chamar sempre que a câmera parar ou
    for removida — senão o cache segura o último frame dela (alguns MB) indefinidamente
    e pode servir imagem velha a um viewer que reconectar antes do primeiro frame novo."""
    with _cache_lock:
        _cache_jpeg.pop(chave, None)


def limpar_cache() -> None:
    """Descarta o cache inteiro — usado quando TODAS as câmeras param de uma vez."""
    with _cache_lock:
        _cache_jpeg.clear()


def gerar_mjpeg(qualidade: int = 75, fps_max: int = 15):
    """Yield JPEG frames como multipart MJPEG."""
    intervalo = 1.0 / max(fps_max, 1)
    while True:
        inicio = time.time()
        frame = estado.obter_frame()
        if frame is not None:
            jpg = _jpeg_cacheado("global", frame, qualidade)
            if jpg is not None:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n"
                    + jpg
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
            jpg = _jpeg_cacheado(camera_id, frame, qualidade)
            if jpg is not None:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n"
                    + jpg
                    + b"\r\n"
                )
        elapsed = time.time() - inicio
        if elapsed < intervalo:
            time.sleep(intervalo - elapsed)


def snapshot_jpeg(qualidade: int = 85) -> bytes | None:
    frame = estado.obter_frame()
    if frame is None:
        return None
    return _jpeg_cacheado("global", frame, qualidade)
