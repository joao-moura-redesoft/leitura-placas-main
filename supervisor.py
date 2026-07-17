"""Supervisor de workers de câmera.

Monitora a saúde de cada Pipeline em execução e reinicia automaticamente
câmeras com thread morta ou sem frames por tempo excessivo.
Usa backoff exponencial para evitar restart em loop.
"""
from __future__ import annotations
import logging
import threading
import time

import banco
import estado

log = logging.getLogger(__name__)

_BACKOFF_INICIAL    = 5.0    # segundos para o primeiro retry
_BACKOFF_MAX        = 300.0  # máximo 5 minutos entre tentativas
_FRESHNESS_ALERTA   = 15.0   # sem frame há Xs → WARNING
_FRESHNESS_REINICIO = 30.0   # sem frame há Xs → reinicia pipeline
_INTERVALO_CHECK    = 5.0    # frequência do loop de supervisão


class WorkerSupervisor:
    def __init__(self) -> None:
        self._parar = threading.Event()
        self._thread: threading.Thread | None = None
        self._backoff_ate: dict[int, float] = {}   # cam_id → ts mínimo para próximo restart
        self._delay_atual: dict[int, float] = {}   # cam_id → delay do próximo backoff
        self._restarts: dict[int, int] = {}        # cam_id → total de restarts
        self._cfg: dict = {}

    def iniciar(self, cfg: dict) -> None:
        self._cfg = cfg
        self._parar.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="alpr-supervisor"
        )
        self._thread.start()
        log.info("WorkerSupervisor iniciado (check a cada %.0fs)", _INTERVALO_CHECK)

    def parar(self) -> None:
        self._parar.set()

    def atualizar_cfg(self, cfg: dict) -> None:
        """Atualiza config usada nos reinícios (chamado após salvar nova config)."""
        self._cfg = cfg

    def health(self) -> dict:
        """Retorna status detalhado por câmera para /api/health."""
        agora = time.time()
        cameras_status = []

        # Import lazy para evitar ciclo de importação
        import pipeline as pl

        for cam in banco.cameras_listar():
            if not cam["ativo"]:
                continue
            cam_id = cam["id"]
            pinst = pl._instancias.get(cam_id)
            thread_viva = bool(
                pinst and pinst._thread and pinst._thread.is_alive()
            )
            ultimo_ts = estado.ultimo_frame_ts.get(cam_id, 0)
            seg_sem_frame = round(agora - ultimo_ts, 1) if ultimo_ts else None

            if not thread_viva:
                st = "parado"
            elif seg_sem_frame is not None and seg_sem_frame > _FRESHNESS_ALERTA:
                st = "sem_frame"
            else:
                st = "ok"

            backoff_restante = max(0.0, round(self._backoff_ate.get(cam_id, 0) - agora, 1))
            cameras_status.append({
                "id":                cam_id,
                "nome":              cam.get("nome") or f"Bomba {cam.get('bomba')} Lado {cam.get('lado')}",
                "status":            st,
                "thread_viva":       thread_viva,
                "ultimo_frame_seg":  seg_sem_frame,
                "restarts":          self._restarts.get(cam_id, 0),
                "backoff_restante_seg": backoff_restante,
                "deteccao_automatica": bool(pinst and pinst.deteccao_automatica) if pinst else None,
            })

        if any(c["status"] == "parado" for c in cameras_status):
            status_geral = "degraded"
        elif any(c["status"] == "sem_frame" for c in cameras_status):
            status_geral = "warning"
        else:
            status_geral = "ok"

        import pipeline as pl  # noqa: F811
        return {
            "status":           status_geral,
            "cameras":          cameras_status,
            "uptime_seg":       round(estado.uptime_segundos(), 1),
            "pipeline_threads": sum(1 for c in cameras_status if c["thread_viva"]),
            "total_restarts":   sum(self._restarts.values()),
        }

    # ── Loop interno ─────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._parar.is_set():
            try:
                self._verificar_workers()
            except Exception as e:
                log.error("Supervisor: erro interno no loop: %s", e)
            self._parar.wait(_INTERVALO_CHECK)

    def _verificar_workers(self) -> None:
        import pipeline as pl

        agora = time.time()
        for cam_id, pinst in list(pl._instancias.items()):
            # Câmeras em modo manual não têm stream contínuo — não monitora freshness
            if not pinst.deteccao_automatica:
                continue

            thread_viva = bool(pinst._thread and pinst._thread.is_alive())

            # 1. Thread morta → reinicia
            if not thread_viva:
                log.error("Camera %d: thread morta — agendando reinício", cam_id)
                self._tentar_reiniciar(cam_id, agora)
                continue

            # 2. Camera recuperada após falha → reseta backoff
            ultimo_ts = estado.ultimo_frame_ts.get(cam_id, 0)
            if ultimo_ts == 0:
                continue  # ainda inicializando, sem frames ainda

            seg_sem_frame = agora - ultimo_ts
            if seg_sem_frame < _FRESHNESS_ALERTA and cam_id in self._delay_atual:
                log.info("Camera %d: operando normalmente — backoff resetado", cam_id)
                self._delay_atual.pop(cam_id, None)
                self._backoff_ate.pop(cam_id, None)
                continue

            # 3. Frame stale → alerta e eventualmente reinicia
            if seg_sem_frame > _FRESHNESS_ALERTA:
                log.warning("Camera %d: sem frame há %.0fs", cam_id, seg_sem_frame)
            if seg_sem_frame > _FRESHNESS_REINICIO:
                log.error(
                    "Camera %d: sem frame há %.0fs — reiniciando pipeline",
                    cam_id, seg_sem_frame,
                )
                self._tentar_reiniciar(cam_id, agora)

    def _tentar_reiniciar(self, cam_id: int, agora: float) -> None:
        # Ainda dentro do período de backoff?
        if agora < self._backoff_ate.get(cam_id, 0):
            restante = self._backoff_ate[cam_id] - agora
            log.debug("Camera %d: backoff ativo — %.0fs restantes", cam_id, restante)
            return

        # Calcula próximo delay (exponencial: 5 → 10 → 20 → ... → 300s)
        delay = self._delay_atual.get(cam_id, _BACKOFF_INICIAL)
        self._backoff_ate[cam_id] = agora + delay
        self._delay_atual[cam_id] = min(delay * 2, _BACKOFF_MAX)
        n = self._restarts.get(cam_id, 0) + 1
        self._restarts[cam_id] = n

        log.warning(
            "Camera %d: reiniciando (tentativa #%d, próximo backoff em %.0fs)",
            cam_id, n, delay,
        )
        try:
            import pipeline as pl
            cam = next((c for c in banco.cameras_listar() if c["id"] == cam_id), None)
            if not cam or not cam["ativo"]:
                log.warning("Camera %d: não encontrada ou inativa — cancelando reinício", cam_id)
                return
            cfg_merged = pl._cfg_para_camera(self._cfg, cam)
            pl.reiniciar_camera(cam_id, cfg_merged)
            log.info("Camera %d: pipeline reiniciado com sucesso", cam_id)
        except Exception as e:
            log.error("Camera %d: falha ao reiniciar: %s", cam_id, e)


supervisor = WorkerSupervisor()
