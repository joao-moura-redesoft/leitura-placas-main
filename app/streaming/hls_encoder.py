"""Encoder HLS por câmera usando FFmpeg como subprocess.

Lê frames do estado global → pipe para FFmpeg → segmentos .ts + playlist .m3u8
em hls/{camera_id}/index.m3u8.

Ativação via config.txt:
    streaming_modo = hls

Requisito externo (sem pip):
    Windows: winget install Gyan.FFmpeg
    Linux:   apt install ffmpeg
    macOS:   brew install ffmpeg
"""
from __future__ import annotations
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from app.core import estado

log = logging.getLogger(__name__)

HLS_DIR = Path("hls")
_FPS       = 15    # frames por segundo enviados ao FFmpeg
_HLS_TIME  = 1     # segundos por segmento .ts
_HLS_LIST  = 6     # quantidade de segmentos mantidos no playlist (~6s de buffer)


def ffmpeg_disponivel() -> bool:
    return shutil.which("ffmpeg") is not None


class _Encoder:
    """Thread que alimenta um processo FFmpeg com frames de uma câmera."""

    def __init__(self, camera_id: int) -> None:
        self.camera_id = camera_id
        self.diretorio = HLS_DIR / str(camera_id)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def iniciar(self) -> None:
        self.diretorio.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True,
            name=f"hls-{self.camera_id}",
        )
        self._thread.start()

    def parar(self) -> None:
        self._stop.set()

    # ── loop interno ──────────────────────────────────────────────────────────

    def _loop(self) -> None:
        log.info("HLS câmera %d: aguardando primeiro frame…", self.camera_id)
        dim = self._aguardar_dimensoes(timeout=60.0)
        if dim is None:
            log.warning(
                "HLS câmera %d: nenhum frame em 60s — encoder cancelado",
                self.camera_id,
            )
            return
        w, h = dim
        log.info(
            "HLS câmera %d: iniciando %dx%d @ %dfps",
            self.camera_id, w, h, _FPS,
        )
        while not self._stop.is_set():
            proc = None
            try:
                proc = self._criar_ffmpeg(w, h)
                self._alimentar(proc, w, h)
            except Exception as e:
                log.error("HLS câmera %d: %s — reiniciando em 5s", self.camera_id, e)
            finally:
                if proc and proc.poll() is None:
                    try:
                        proc.stdin.close()
                        proc.wait(timeout=3)
                    except Exception:
                        proc.kill()
                        try:
                            proc.wait(timeout=2)
                        except Exception:
                            pass
            if not self._stop.is_set():
                time.sleep(5)

    def _aguardar_dimensoes(self, timeout: float) -> tuple[int, int] | None:
        fim = time.time() + timeout
        while not self._stop.is_set() and time.time() < fim:
            f = estado.obter_frame_camera(self.camera_id)
            if f is not None:
                h, w = f.shape[:2]
                return w, h
            time.sleep(0.5)
        return None

    def _criar_ffmpeg(self, w: int, h: int) -> subprocess.Popen:
        m3u8 = str(self.diretorio / "index.m3u8")
        seg  = str(self.diretorio / "seg%04d.ts")
        cmd = [
            "ffmpeg", "-y",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{w}x{h}", "-r", str(_FPS),
            "-i", "pipe:0",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-tune", "zerolatency", "-crf", "28",
            "-g", str(_FPS * 2), "-sc_threshold", "0",
            "-hls_time",         str(_HLS_TIME),
            "-hls_list_size",    str(_HLS_LIST),
            "-hls_flags",        "delete_segments+append_list",
            "-hls_segment_filename", seg,
            m3u8,
        ]
        return subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _alimentar(self, proc: subprocess.Popen, w: int, h: int) -> None:
        intervalo = 1.0 / _FPS
        blank = np.zeros((h, w, 3), dtype=np.uint8)
        while not self._stop.is_set():
            t0 = time.time()
            frame = estado.obter_frame_camera(self.camera_id)
            f = frame if frame is not None else blank
            if f.shape[:2] != (h, w):
                f = cv2.resize(f, (w, h))
            try:
                proc.stdin.write(f.tobytes())
            except (BrokenPipeError, OSError):
                break
            rem = intervalo - (time.time() - t0)
            if rem > 0:
                time.sleep(rem)


class HLSManager:
    """Gerencia encoders HLS de todas as câmeras ativas.

    Uso em servidor.py:
        hls_manager.iniciar(cameras)   # no lifespan startup
        hls_manager.parar()            # no lifespan shutdown
    """

    def __init__(self) -> None:
        self._encoders: dict[int, _Encoder] = {}
        self._ativo = False

    def iniciar(self, cameras: list[dict]) -> bool:
        """Inicia encoders para todas as câmeras ativas. Retorna False se FFmpeg ausente."""
        if not ffmpeg_disponivel():
            log.warning(
                "FFmpeg não encontrado — streaming HLS indisponível.\n"
                "  Windows: winget install Gyan.FFmpeg\n"
                "  Linux:   apt install ffmpeg"
            )
            return False
        self._ativo = True
        HLS_DIR.mkdir(exist_ok=True)
        for cam in cameras:
            if cam.get("ativo"):
                self._iniciar_encoder(cam["id"])
        return True

    def adicionar_camera(self, camera_id: int) -> None:
        """Chame ao ativar uma câmera via API."""
        if self._ativo:
            self._iniciar_encoder(camera_id)

    def remover_camera(self, camera_id: int) -> None:
        """Chame ao desativar uma câmera via API."""
        enc = self._encoders.pop(camera_id, None)
        if enc:
            enc.parar()

    def parar(self) -> None:
        for enc in self._encoders.values():
            enc.parar()
        self._encoders.clear()
        self._ativo = False

    def ativo(self) -> bool:
        return self._ativo

    # ── interno ───────────────────────────────────────────────────────────────

    def _iniciar_encoder(self, camera_id: int) -> None:
        if camera_id in self._encoders:
            return
        enc = _Encoder(camera_id)
        enc.iniciar()
        self._encoders[camera_id] = enc
        log.info("HLS: encoder iniciado para câmera %d", camera_id)


hls_manager = HLSManager()
