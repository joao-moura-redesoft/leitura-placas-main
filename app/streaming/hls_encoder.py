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
from app.core import threads

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

    def parar(self, timeout: float = 8.0) -> bool:
        """Sinaliza parada e ESPERA a thread encerrar o FFmpeg. False no timeout.

        O `subprocess` só é encerrado no `finally` do laço, que roda numa thread DAEMON.
        Com `parar()` retornando na hora e o interpretador saindo em seguida, a thread era
        morta antes do `finally` e o `ffmpeg` ficava ÓRFÃO gravando em `hls/{id}/` — no
        Windows não há kill em cascata do filho, então cada reinício do serviço acumulava um
        ffmpeg por câmera. (Auditoria 27/08/2026, achado M8.)
        """
        self._stop.set()
        return threads.encerrar_thread(self._thread, timeout, lambda: log.warning(
            "HLS câmera %d: encoder não encerrou em %.0fs — o processo ffmpeg pode "
            "ficar órfão", self.camera_id, timeout))

    def morto(self) -> bool:
        """Thread já criada e não mais viva — encoder que desistiu e não volta sozinho."""
        return self._thread is not None and not self._thread.is_alive()

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
                if proc:
                    # `stdin.close()` FORA do `if poll() is None`: quando o ffmpeg já morreu
                    # (o caso comum — `_alimentar` sai por BrokenPipeError), o descritor do
                    # pipe nunca era fechado, e o laço de retry a cada 5 s vazava um fd por
                    # volta até o processo bater no limite do sistema.
                    try:
                        if proc.stdin and not proc.stdin.closed:
                            proc.stdin.close()
                    except Exception:
                        pass
                    if proc.poll() is None:
                        try:
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
        # RLock (não Lock comum): `revisar()`/`parar()` chamam `remover_camera()` no MESMO
        # objeto enquanto já seguram o lock — um Lock comum travaria a própria thread.
        #
        # Protege `_encoders`/`_ativo` contra mutação concorrente: o supervisor chama
        # `revisar()` a cada 5s numa thread de fundo, e uma request HTTP (config_salvar
        # trocando `streaming_modo`, ou o CRUD de câmeras) pode chamar `parar()`/`iniciar()`/
        # `adicionar_camera()`/`remover_camera()` ao mesmo tempo. Sem lock, `revisar()`
        # podia iterar `_encoders` bem no meio de um `parar()+iniciar()` de outra thread e
        # deixar um `_Encoder` (e o processo ffmpeg dele) órfão — sem referência em
        # `_encoders`, `parar()`/`remover_camera()` nunca mais conseguem pará-lo.
        # (Achado do review de 28/08/2026.)
        self._lock = threading.RLock()

    def iniciar(self, cameras: list[dict]) -> bool:
        """Inicia encoders para todas as câmeras ativas. Retorna False se FFmpeg ausente."""
        if not ffmpeg_disponivel():
            log.warning(
                "FFmpeg não encontrado — streaming HLS indisponível.\n"
                "  Windows: winget install Gyan.FFmpeg\n"
                "  Linux:   apt install ffmpeg"
            )
            return False
        with self._lock:
            self._ativo = True
            HLS_DIR.mkdir(exist_ok=True)
            for cam in cameras:
                if cam.get("ativo"):
                    self._iniciar_encoder(cam["id"])
        return True

    def revisar(self, cameras) -> None:
        """Sobe encoder faltante e recria os que morreram. Chamado pelo supervisor.

        Sem isto, `adicionar_camera`/`remover_camera` eram CÓDIGO MORTO — nada no projeto os
        chamava —, então cadastrar câmera com `streaming_modo=hls` nunca criava encoder.
        """
        with self._lock:
            if not self._ativo:
                return
            ativas = {c["id"] for c in cameras if c.get("ativo")}
            for camera_id in ativas:
                self._iniciar_encoder(camera_id)
            for camera_id in list(self._encoders):
                if camera_id not in ativas:
                    self.remover_camera(camera_id)

    def adicionar_camera(self, camera_id: int) -> None:
        """Chame ao ativar uma câmera via API."""
        with self._lock:
            if self._ativo:
                self._iniciar_encoder(camera_id)

    def remover_camera(self, camera_id: int) -> None:
        """Chame ao desativar uma câmera via API."""
        with self._lock:
            enc = self._encoders.pop(camera_id, None)
        # `enc.parar()` (join de até 8s) fica FORA do lock quando chamado direto — só quem
        # chama de dentro de `revisar()` (que já segura o lock) paga o join sob lock, o que
        # é aceitável por ser um tick periódico raro de remover câmera morta.
        if enc:
            enc.parar()

    def parar(self) -> None:
        with self._lock:
            # Para TODOS antes de esperar qualquer um: sinalizar em série e joinar em série
            # somaria os timeouts (8 s por câmera).
            encoders = list(self._encoders.values())
            for enc in encoders:
                enc._stop.set()
            self._encoders.clear()
            self._ativo = False
        # Os joins ficam FORA do lock: a mutação de `_encoders`/`_ativo` já terminou acima,
        # e os encoders parados não estão mais em `_encoders` — outra thread pode voltar a
        # chamar `iniciar()`/`ativo()` sem esperar os até 8s por câmera aqui.
        for enc in encoders:
            enc.parar()

    def ativo(self) -> bool:
        with self._lock:
            return self._ativo

    # ── interno ───────────────────────────────────────────────────────────────

    def _iniciar_encoder(self, camera_id: int) -> None:
        """Assume que `self._lock` já está seguro pelo chamador — nunca chamar direto."""
        atual = self._encoders.get(camera_id)
        if atual is not None:
            if not atual.morto():
                return
            # Encoder que saiu por timeout de primeiro frame (`_loop` faz `return` após 60 s
            # sem quadro) continuava no dicionário, e este `return` antecipado impedia
            # qualquer recriação: a câmera ficava SEM HLS até reiniciar o processo, mesmo
            # depois de voltar a transmitir.
            log.info("HLS: encoder da câmera %d estava morto — recriando", camera_id)
            self._encoders.pop(camera_id, None)
        enc = _Encoder(camera_id)
        enc.iniciar()
        self._encoders[camera_id] = enc
        log.info("HLS: encoder iniciado para câmera %d", camera_id)


hls_manager = HLSManager()
