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
_fps_global: float = 0.0   # só para o ramo camera_id=None de atualizar_fps (hoje sem chamador)
iniciado_em: float = time.time()
emissoes_recentes: dict[int, list] = {}  # camera_db_id -> [(placa, timestamp), ...]
frames_processados: int = 0
# `pipeline_rodando()`/`camera_conectada()`/`fps_atual()` (funções, abaixo) são agregados
# COMPUTADOS a partir dos dicts por câmera — "algum pipeline rodando", "alguma câmera
# conectada", "soma de fps". Não são globals mantidas em sincronia à mão: chegaram a ser, e
# `esquecer_camera` (abaixo) esquecia de recomputá-las depois do `pop`, deixando o agregado
# fantasma até a próxima chamada de `marcar_pipeline`/`marcar_conexao`. Virar função elimina
# a classe inteira do problema — não há mais o que dessincronizar.
pipelines_rodando: dict[int, bool] = {}   # camera_db_id -> pipeline vivo
cameras_conectadas: dict[int, bool] = {}  # camera_db_id -> conexão de câmera ok
fps_cameras: dict[int, float] = {}        # camera_db_id -> fps daquela câmera
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


def registrar_frame_camera(camera_id: int, frame, ts: float | None = None) -> None:
    """Publica o frame anotado da câmera e atualiza o relógio de frescor.

    `ts` é o instante em que a FONTE produziu o quadro — não o instante em que o pipeline
    publicou. Os dois divergem no caminho de republicação (`pipeline._loop_camera` reemite o
    mesmo quadro quando a câmera não entregou nada novo, para o stream não piscar), e é
    justamente essa divergência que o watchdog precisa enxergar: medindo a SAÍDA, uma câmera
    congelada parecia saudável para sempre. Omitir `ts` mantém o comportamento antigo (agora),
    que é o correto para quem acabou de ler um quadro novo da câmera.
    """
    with lock:
        frames_cameras[camera_id] = frame
        ultimo_frame_ts[camera_id] = time.time() if ts is None else ts


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
        pipelines_rodando.pop(camera_id, None)
        cameras_conectadas.pop(camera_id, None)
        fps_cameras.pop(camera_id, None)


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


def atualizar_fps(valor: float, camera_id: int | None = None) -> None:
    """Registra o FPS. Com `camera_id`, guarda POR CÂMERA (o correto num servidor
    multi-câmera); `fps_atual()` devolve a soma, que é o número que o dashboard quer dizer.

    Escreve SOB O LOCK — era o único escritor do módulo que não pegava, num dict que
    outras threads leem. (Auditoria 27/08/2026, achado M9.)
    """
    global _fps_global
    with lock:
        if camera_id is None:
            _fps_global = valor
            return
        fps_cameras[camera_id] = valor


def _fps_atual_sob_lock() -> float:
    """Sem lock próprio — para `fps_atual()` e `snapshot_status()` (que já segura o
    lock, não-reentrante) compartilharem a MESMA fórmula em vez de reimplementá-la."""
    return round(sum(fps_cameras.values()), 1) if fps_cameras else _fps_global


def _pipeline_rodando_sob_lock() -> bool:
    return bool(pipelines_rodando)


def _camera_conectada_sob_lock() -> bool:
    return any(cameras_conectadas.values())


def fps_atual() -> float:
    """Soma do fps de cada câmera viva — o número que o dashboard quer dizer."""
    with lock:
        return _fps_atual_sob_lock()


def pipeline_rodando() -> bool:
    """"Algum pipeline está rodando" — computado a partir de `pipelines_rodando`, nunca
    mantido à parte (ver o comentário na declaração dos dicts, acima)."""
    with lock:
        return _pipeline_rodando_sob_lock()


def camera_conectada() -> bool:
    """"Alguma câmera está conectada" — computado a partir de `cameras_conectadas`."""
    with lock:
        return _camera_conectada_sob_lock()


def marcar_pipeline(camera_db_id: int, rodando: bool) -> None:
    """Estado do pipeline DESTA câmera. `pipeline_rodando()` devolve "algum está rodando".

    Antes desta função, `pipeline_rodando` era um booleano único que todo Pipeline
    escrevia: parar a câmera 2 fazia o painel anunciar "pipeline parado" com outras cinco
    no ar. Mesma história de `camera_conectada`, que acusava a câmera errada. (Auditoria
    27/08/2026, achado M9.)
    """
    with lock:
        if rodando:
            pipelines_rodando[camera_db_id] = True
        else:
            pipelines_rodando.pop(camera_db_id, None)
            fps_cameras.pop(camera_db_id, None)


def marcar_conexao(camera_db_id: int, conectada: bool) -> None:
    """Conexão DESTA câmera. `camera_conectada()` devolve "alguma está conectada"."""
    with lock:
        cameras_conectadas[camera_db_id] = conectada


def cameras_no_ar() -> dict:
    """Visão por câmera, para o dashboard não depender de um booleano agregado."""
    with lock:
        return {
            "pipelines": dict(pipelines_rodando),
            "conectadas": dict(cameras_conectadas),
            "fps": dict(fps_cameras),
        }


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
    # `lock` não é reentrante — não pode chamar `pipeline_rodando()`/`fps_atual()`/
    # `camera_conectada()` daqui dentro (elas tentam pegar o mesmo lock de novo). Os
    # `_..._sob_lock()` existem por isso: mesma fórmula das três, sem lock próprio.
    with lock:
        return {
            "pipeline": _pipeline_rodando_sob_lock(),
            "fps": _fps_atual_sob_lock(),
            "uptime_seg": uptime_segundos(),
            "frames_processados": frames_processados,
            "camera_conectada": _camera_conectada_sob_lock(),
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
