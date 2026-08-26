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

# Quanto `fechar()` espera a thread leitora antes de desistir de liberar o cap. Com folga
# sobre os 4s de CAP_PROP_READ_TIMEOUT_MSEC: a leitora só precisa de um `read()` para
# notar `_parar_leitura`. Constante (e não literal) para os testes conseguirem exercitar
# o caminho do timeout sem esperar 6s de verdade.
TIMEOUT_JOIN_LEITORA_SEG = 6.0


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

        # Timeout de leitura: cap.read() retorna após 4s sem frame em vez de bloquear para
        # sempre. É o que dá a `fechar()` a chance de ver a thread leitora encerrar.
        # `set` devolve False quando o backend não implementa a propriedade — e essa é
        # justamente a situação em que `fechar()` vai bater no timeout do join e ter de
        # segurar o cap. Logar aqui é o que liga uma coisa à outra: sem isto, o "não
        # encerrou em 6s" mais adiante aparece sem causa visível.
        if self.tipo in ("rtsp", "intelbras"):
            try:
                if not self.cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 4000):
                    log.warning(
                        "Backend não aceitou CAP_PROP_READ_TIMEOUT_MSEC — cap.read() pode "
                        "bloquear sem teto se a câmera parar de responder"
                    )
            except Exception as e:
                log.warning("Não foi possível definir o timeout de leitura (%s)", e)

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

    def fechar(self) -> bool:
        """Encerra a thread leitora e libera o cap. Devolve False, SEM liberar nada,
        se a leitora não morrer dentro do timeout.

        Aguardar a leitora ANTES de liberar o cap não é zelo: chamar `cap.release()`
        enquanto `cap.read()` corre em outra thread derruba o PROCESSO INTEIRO com
        access violation no Windows+FFmpeg+RTSP — crash nativo, sem traceback Python,
        o servidor simplesmente some. O `join` sempre esteve aqui, mas o retorno dele
        era ignorado: passado o timeout o código seguia para o `release()` do mesmo
        jeito, que é exatamente a condição que ele existia para evitar. Bastava a
        leitora estar presa num `read()` (RTSP remoto instável, câmera que sumiu da
        rede) para editar/remover uma câmera matar o servidor.

        `CAP_PROP_READ_TIMEOUT_MSEC` (definido em `abrir`) deveria limitar o `read()`
        a 4s, mas é best-effort — o backend pode recusar a propriedade, e aí não há
        teto nenhum. Não dá para apostar o processo nisso.

        Quando a leitora não confirma parada, o cap FICA vivo: a conexão RTSP vaza
        até alguém chamar `fechar()` de novo (o supervisor faz isso a cada ciclo, via
        `parar_camera`), e nessa hora a leitura já terá retornado e o release é
        seguro. Uma conexão pendurada por alguns segundos é incomparavelmente melhor
        que um processo morto.
        """
        self._parar_leitura.set()
        if self._reader is not None:
            self._reader.join(timeout=TIMEOUT_JOIN_LEITORA_SEG)
            if self._reader.is_alive():
                log.error(
                    "Câmera (%s): thread leitora não encerrou a tempo — provavelmente "
                    "presa em cap.read(). NÃO liberando o cap: release() com a leitura "
                    "em andamento derruba o processo. A conexão fica retida até a "
                    "próxima tentativa de fechar.",
                    self.tipo,
                )
                return False
            self._reader = None
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None
        with self._frame_lock:
            self._ultimo_frame = None
        return True

    def leitora_viva(self) -> bool:
        """Se a thread leitora ainda está no ar — é ela que impede liberar o cap."""
        return self._reader is not None and self._reader.is_alive()

    def reconectar(self, tentativas: int = 2) -> bool:
        # Sem o guarda, uma reconexão em cima de uma leitora presa abriria uma SEGUNDA
        # conexão RTSP para a mesma câmera física (que a Intelbras não aceita) e ainda
        # sobrescreveria `self.cap`, tornando o cap antigo inalcançável para sempre.
        if not self.fechar():
            log.warning("Reconexão adiada: a conexão anterior ainda não pôde ser liberada")
            return False
        for n in range(tentativas):
            try:
                self.abrir()
                return True
            except Exception as e:
                log.warning("Reconexão %d/%d falhou: %s", n + 1, tentativas, e)
                if n + 1 < tentativas:
                    time.sleep(5)
        return False


# Câmeras de vida curta (`capturar_frame_unico`/`capturar_teste`) que não puderam ser
# fechadas com segurança. Elas PRECISAM continuar referenciadas: sem isto o objeto sai de
# escopo ao fim da função, o coletor de lixo destrói o `cv2.VideoCapture` e o destrutor
# nativo chama release() — com a leitora ainda dentro de `cap.read()`, que é o access
# violation que `Camera.fechar` recusa a provocar. Só que agora ele viria depois, em outra
# thread e sem relação visível com nada. A lista é o que segura a referência até dar.
_pendentes_fechar: list["Camera"] = []
_pendentes_lock = threading.Lock()


def _drenar_pendentes() -> None:
    """Tenta de novo liberar as câmeras que ficaram retidas. Barato: quem ainda tem
    leitora viva é pulado sem pagar o join de 6s."""
    with _pendentes_lock:
        if not _pendentes_fechar:
            return
        restantes = []
        for cam in _pendentes_fechar:
            if cam.leitora_viva() or not cam.fechar():
                restantes.append(cam)
            else:
                log.info("Câmera retida anteriormente foi liberada")
        _pendentes_fechar[:] = restantes


def fechar_ou_adiar(cam: "Camera", contexto: str) -> None:
    """Fecha a câmera; se a leitora não morreu, guarda a instância para tentar depois."""
    _drenar_pendentes()
    if cam.fechar():
        return
    with _pendentes_lock:
        _pendentes_fechar.append(cam)
    log.error(
        "%s: conexão retida (thread leitora presa) — %d câmera(s) aguardando liberação. "
        "A próxima captura tenta de novo.",
        contexto, len(_pendentes_fechar),
    )


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

    fechar_ou_adiar(cam, "capturar_frame_unico")
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

    fechar_ou_adiar(cam, "capturar_teste")
    if frame is None:
        return False, "Câmera abriu mas não retornou frame", None

    ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    if not ok:
        return False, "Falha ao codificar JPEG", None
    return True, "ok", jpg.tobytes()
