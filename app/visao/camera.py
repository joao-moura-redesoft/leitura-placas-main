"""Captura de vídeo via OpenCV.

Tipos suportados:
  - usb       : webcam USB (/dev/videoN)
  - csi       : Raspberry Pi Camera (libcamera via GStreamer)
  - rtsp      : URL RTSP genérica
  - intelbras : câmeras IP Intelbras (linha VIP — protocolo Dahua)

Documentação oficial Intelbras (fórum/manual):
  - Formato padrão (maioria dos modelos VIP):
      rtsp://USER:PASS@HOST:554/cam/realmonitor?channel=N&subtype=S
  - Formato legado (VIP 1120/1220/1130):
      rtsp://HOST:554/user=USER&password=PASS&channel=N&stream=S.sdp?
  - subtype/stream: 0 = main (alta resolução), 1 = sub (baixa, menos CPU)
  - Porta RTSP padrão: 554
  - Modelos VIP 1120 B / 1120 D NÃO suportam RTSP (somente ONVIF/web)

Arquitetura de leitura:
  Uma thread dedicada (_reader_loop) consome o buffer da câmera o mais
  rápido possível, mantendo sempre só o frame mais recente em memória.
  Isso evita o acúmulo de delay observado em streams RTSP de longa duração.
"""
from __future__ import annotations
import logging
import platform
import threading
import time
from urllib.parse import quote

import cv2

_USB_BACKEND = cv2.CAP_DSHOW if platform.system() == "Windows" else cv2.CAP_V4L2

log = logging.getLogger(__name__)


def url_intelbras(
    host: str,
    porta: int = 554,
    usuario: str = "admin",
    senha: str = "",
    canal: int = 1,
    subtype: int = 1,
    formato: str = "padrao",
) -> str:
    u = quote(usuario, safe="")
    p = quote(senha, safe="")
    if formato == "legado":
        return (
            f"rtsp://{host}:{porta}/user={u}&password={p}"
            f"&channel={canal}&stream={subtype}.sdp?"
        )
    return (
        f"rtsp://{u}:{p}@{host}:{porta}/cam/realmonitor"
        f"?channel={canal}&subtype={subtype}"
    )


class Camera:
    def __init__(
        self,
        tipo: str = "usb",
        indice: str = "0",
        largura: int = 1280,
        altura: int = 720,
        fps: int = 15,
        intelbras: dict | None = None,
        log_abertura_debug: bool = False,
    ):
        self.tipo = tipo
        self.indice = indice
        self.largura = largura
        self.altura = altura
        self.fps = fps
        # Abrir/fechar RTSP em laço (coletor de dataset) não é evento — ver
        # `capturar_frame_unico`. Parâmetro, e não atributo mexido de fora depois de
        # construir, para que a decisão seja visível em quem cria a câmera.
        self.log_abertura_debug = log_abertura_debug
        self.intelbras = intelbras or {}
        self.cap: cv2.VideoCapture | None = None

        self._ultimo_frame = None
        self._frame_lock = threading.Lock()
        self._parar_leitura = threading.Event()
        self._reader: threading.Thread | None = None

    def _origem_rtsp(self) -> str:
        # rtsp e intelbras usam os mesmos parâmetros de host/canal/formato
        if self.tipo in ("intelbras", "rtsp") and self.intelbras.get("host"):
            return url_intelbras(
                host=self.intelbras.get("host", ""),
                porta=int(self.intelbras.get("porta", 554)),
                usuario=self.intelbras.get("usuario", "admin"),
                senha=self.intelbras.get("senha", ""),
                canal=int(self.intelbras.get("canal", 1)),
                subtype=int(self.intelbras.get("subtype", 1)),
                formato=self.intelbras.get("formato", "padrao"),
            )
        return self.indice

    def _log_abertura(self, msg: str, *args) -> None:
        log.log(logging.DEBUG if self.log_abertura_debug else logging.INFO, msg, *args)

    def abrir(self) -> None:
        if self.tipo in ("rtsp", "intelbras"):
            origem = self._origem_rtsp()
            senha = self.intelbras.get("senha", "") or "___NADA___"
            log_origem = origem.replace(senha, "***")
            transporte = self.intelbras.get("rtsp_transporte", "tcp") or "tcp"
            import os as _os
            _os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = f"rtsp_transport;{transporte}"
            self._log_abertura("Abrindo stream: %s (transporte=%s)", log_origem, transporte)
            self.cap = cv2.VideoCapture(origem, cv2.CAP_FFMPEG)
            try:
                self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
        elif self.tipo == "csi":
            pipeline = (
                f"libcamerasrc ! video/x-raw,width={self.largura},height={self.altura},"
                f"framerate={self.fps}/1 ! videoconvert ! appsink"
            )
            self.cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
        else:
            indice_int = int(self.indice) if str(self.indice).strip().isdigit() else 0
            cap = cv2.VideoCapture(indice_int, _USB_BACKEND)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.largura)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.altura)
            cap.set(cv2.CAP_PROP_FPS, self.fps)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            self.cap = cap

        if not self.cap or not self.cap.isOpened():
            raise RuntimeError(f"Não foi possível abrir a câmera ({self.tipo})")

        # Timeout de leitura: cap.read() retorna após 4s sem frame em vez de bloquear para sempre.
        # Isso permite que fechar() aguarde a thread leitora encerrar sem race condition.
        if self.tipo in ("rtsp", "intelbras"):
            try:
                self.cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 4000)
            except Exception:
                pass

        self._log_abertura("Câmera aberta: tipo=%s %dx%d@%d",
                           self.tipo, self.largura, self.altura, self.fps)

        # Inicia thread leitora que drena o buffer continuamente
        self._parar_leitura.clear()
        self._ultimo_frame = None
        self._reader = threading.Thread(
            target=self._reader_loop, daemon=True, name="camera-reader"
        )
        self._reader.start()

    def _reader_loop(self) -> None:
        """Drena o buffer da câmera o mais rápido possível.

        Mantém apenas o frame mais recente em memória. Sem sleep proposital —
        o objetivo é nunca deixar frames se acumularem no buffer do FFmpeg/V4L2.
        """
        while not self._parar_leitura.is_set():
            cap = self.cap
            if cap is None:
                break
            try:
                ok, frame = cap.read()
            except Exception:
                break
            if ok:
                with self._frame_lock:
                    self._ultimo_frame = frame
            else:
                # Câmera parou de responder — sinaliza com None para o pipeline reconectar
                with self._frame_lock:
                    self._ultimo_frame = None
                time.sleep(0.05)

    def ler(self):
        """Retorna o frame mais recente (nunca frames antigos acumulados)."""
        with self._frame_lock:
            return self._ultimo_frame

    def fechar(self) -> None:
        self._parar_leitura.set()
        # Aguarda a thread leitora encerrar ANTES de liberar o cap.
        # Chamar cap.release() enquanto cap.read() está em andamento em outra
        # thread causa crash nativo (access violation) no Windows+FFmpeg+RTSP.
        # O CAP_PROP_READ_TIMEOUT_MSEC garante que cap.read() retorna em ≤4s.
        if self._reader is not None:
            self._reader.join(timeout=6)
            self._reader = None
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None
        with self._frame_lock:
            self._ultimo_frame = None

    def reconectar(self, tentativas: int = 2) -> bool:
        self.fechar()
        for n in range(tentativas):
            try:
                self.abrir()
                return True
            except Exception as e:
                log.warning("Reconexão %d/%d falhou: %s", n + 1, tentativas, e)
                if n + 1 < tentativas:
                    time.sleep(5)
        return False


def capturar_frame_unico(
    tipo: str,
    indice: str,
    largura: int = 1280,
    altura: int = 720,
    fps: int = 15,
    intelbras: dict | None = None,
    silencioso: bool = False,
):
    """Conecta, captura UM frame e desconecta. Retorna numpy array ou None.

    `silencioso` desce "Abrindo stream"/"Câmera aberta" para DEBUG. Abrir e fechar RTSP
    é evento digno de INFO quando um pipeline sobe; quando é o coletor de dataset fazendo
    isso a cada `captura_dataset_intervalo_seg`, por câmera, viram quatro linhas por
    minuto que só repetem que o relógio bateu. A falha continua em ERROR nos dois casos.
    """
    cam = Camera(tipo=tipo, indice=indice, largura=largura, altura=altura, fps=fps,
                 intelbras=intelbras, log_abertura_debug=silencioso)
    try:
        cam.abrir()
    except Exception as e:
        log.error("capturar_frame_unico: falha ao abrir câmera: %s", e)
        return None

    frame = None
    for _ in range(150):   # até 15s esperando primeiro frame válido
        frame = cam.ler()
        if frame is not None:
            break
        time.sleep(0.1)

    cam.fechar()
    return frame


def capturar_teste(
    tipo: str,
    indice: str,
    largura: int = 1280,
    altura: int = 720,
    fps: int = 15,
    intelbras: dict | None = None,
) -> tuple[bool, str, bytes | None]:
    """Abre a câmera, captura UM frame e fecha. Para testes de conexão na UI."""
    cam = Camera(tipo=tipo, indice=indice, largura=largura, altura=altura, fps=fps, intelbras=intelbras)
    try:
        cam.abrir()
    except Exception as e:
        return False, f"Falha ao abrir: {e}", None

    # Aguarda a thread leitora capturar o primeiro frame válido (até 15s para RTSP)
    frame = None
    for _ in range(150):
        frame = cam.ler()
        if frame is not None:
            break
        time.sleep(0.1)

    cam.fechar()
    if frame is None:
        return False, "Câmera abriu mas não retornou frame", None

    ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    if not ok:
        return False, "Falha ao codificar JPEG", None
    return True, "ok", jpg.tobytes()
