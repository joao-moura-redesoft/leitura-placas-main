"""Estado global compartilhado entre threads (pipeline e FastAPI)."""
from __future__ import annotations
import logging
import threading
import time
from collections import deque

lock = threading.Lock()
frame_atual = None
frames_cameras: dict = {}        # camera_db_id -> frame (anotado, com bboxes p/ stream)
frames_cameras_limpos: dict = {} # camera_db_id -> frame limpo (sem overlay, p/ ler-placa)
ultimo_frame_ts: dict = {}       # camera_db_id -> timestamp do último frame válido
ultimas_deteccoes: deque = deque(maxlen=20)
logs_recentes: deque = deque(maxlen=200)
fps_atual: float = 0.0
iniciado_em: float = time.time()
pipeline_rodando: bool = False
emissoes_recentes: dict[int, list] = {}  # camera_db_id -> [(placa, timestamp), ...]
frames_processados: int = 0
camera_conectada: bool = False
modelo_carregado: bool = False
ocr_engine_ativo: str = ""
camera_tipo: str = ""
ultimo_crop_ocr_jpg: bytes | None = None  # último crop enviado ao Tesseract (debug)
instalando_pacote: str = ""  # nome do pacote pip sendo instalado no momento ("" = ocioso)
ambiente: dict = {}          # último perfil de cena do ajuste adaptativo (por câmera + "ultimo")


def registrar_frame(frame) -> None:
    global frame_atual
    with lock:
        frame_atual = frame


def obter_frame():
    with lock:
        return frame_atual


def registrar_frame_camera(camera_id: int, frame) -> None:
    with lock:
        frames_cameras[camera_id] = frame
        ultimo_frame_ts[camera_id] = time.time()


def obter_frame_camera(camera_id: int):
    with lock:
        return frames_cameras.get(camera_id)


def registrar_frame_camera_limpo(camera_id: int, frame) -> None:
    with lock:
        frames_cameras_limpos[camera_id] = frame


def obter_frame_camera_limpo(camera_id: int):
    with lock:
        return frames_cameras_limpos.get(camera_id)


def esquecer_camera(camera_id: int) -> None:
    """Descarta todo estado em memória de uma câmera removida.

    Os dicts de frame são indexados por camera_db_id e nunca eram limpos: apagar uma
    câmera deixava o último frame dela (alguns MB, dois por câmera) preso para sempre,
    e o `ultimo_frame_ts` órfão faria um id reaproveitado parecer ter imagem fresca
    antes de qualquer captura.
    """
    with lock:
        frames_cameras.pop(camera_id, None)
        frames_cameras_limpos.pop(camera_id, None)
        ultimo_frame_ts.pop(camera_id, None)
        emissoes_recentes.pop(camera_id, None)
        ambiente.pop(camera_id, None)


def adicionar_deteccao(deteccao: dict) -> None:
    with lock:
        ultimas_deteccoes.appendleft(deteccao)


def listar_recentes() -> list[dict]:
    with lock:
        return list(ultimas_deteccoes)


def obter_emissoes_recentes(camera_db_id: int, cooldown_seg: float) -> list[tuple[str, float]]:
    """Emissões dessa câmera ainda dentro da janela de cooldown (poda as expiradas)."""
    agora = time.time()
    with lock:
        vivas = [e for e in emissoes_recentes.get(camera_db_id, []) if agora - e[1] < cooldown_seg]
        emissoes_recentes[camera_db_id] = vivas
        return list(vivas)


def registrar_emissao(camera_db_id: int, placa: str) -> None:
    """Registra (ou renova, se já presente) o timestamp desta placa — chamado tanto numa
    emissão nova quanto quando uma leitura parecida repete dentro do cooldown, para a
    janela deslizar e cobrir sequências de retries mais longas que um único cooldown_seg."""
    with lock:
        lst = emissoes_recentes.setdefault(camera_db_id, [])
        lst[:] = [e for e in lst if e[0] != placa]
        lst.append((placa, time.time()))


def atualizar_fps(valor: float) -> None:
    global fps_atual
    fps_atual = valor


def registrar_ambiente(camera_id: int, perfil: str, **metricas) -> None:
    """Guarda o perfil de cena detectado pelo ajuste adaptativo (por câmera e global)."""
    dado = {"perfil": perfil, "camera_id": camera_id, **metricas}
    with lock:
        ambiente[camera_id] = dado
        ambiente["ultimo"] = dado


def uptime_segundos() -> float:
    return time.time() - iniciado_em


def incrementar_frame() -> None:
    global frames_processados
    with lock:
        frames_processados += 1


def snapshot_status() -> dict:
    with lock:
        return {
            "pipeline": pipeline_rodando,
            "fps": fps_atual,
            "uptime_seg": uptime_segundos(),
            "frames_processados": frames_processados,
            "camera_conectada": camera_conectada,
            "camera_tipo": camera_tipo,
            "modelo_carregado": modelo_carregado,
            "ocr_engine": ocr_engine_ativo,
            "instalando_pacote": instalando_pacote,
            "ambiente": ambiente.get("ultimo"),
        }


def listar_logs() -> list[dict]:
    with lock:
        return list(logs_recentes)


def limpar_logs() -> None:
    with lock:
        logs_recentes.clear()


class RingLogHandler(logging.Handler):
    """Captura logs em buffer circular para exibir no dashboard."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        with lock:
            logs_recentes.appendleft({
                "ts": time.time(),
                "level": record.levelname,
                "name": record.name,
                "msg": msg,
            })


def registrar_crop_ocr(img_bgr_ou_gray) -> None:
    """Armazena o último crop enviado ao OCR como JPEG para debug."""
    global ultimo_crop_ocr_jpg
    try:
        import cv2
        ok, buf = cv2.imencode(".jpg", img_bgr_ou_gray, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if ok:
            with lock:
                ultimo_crop_ocr_jpg = buf.tobytes()
    except Exception:
        pass


def instalar_log_handler() -> None:
    """Adiciona o RingLogHandler ao root logger (idempotente)."""
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, RingLogHandler):
            return
    h = RingLogHandler()
    h.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(h)
