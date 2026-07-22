"""Rastreador de veículos entre frames para reduzir chamadas OCR.

Implementação em duas camadas:
  1. ByteTrack (boxmot)  — se instalado: tracker robusto com Kalman filter
  2. _IoUTracker         — fallback interno puro Python: matching por IoU simples

Em ambos os casos tracker.ativo() retorna True e a interface é idêntica.
O pipeline usa `tracker=None` apenas quando tracker_ativo=nao no config.

Redução de OCR:
  - OCR roda no primeiro frame de cada track (novo veículo)
  - OCR roda novamente a cada `ocr_a_cada_n_frames` frames do mesmo track
  - Emite quando o track acumula `votos_emitir` leituras concordantes
"""
from __future__ import annotations
import logging
from collections import Counter

import numpy as np

log = logging.getLogger(__name__)


# ── Tracker IoU interno (fallback zero-dependências) ─────────────────────────

class _IoUTracker:
    """
    Tracker simples por IoU — sem dependências externas.
    Suficiente para câmeras fixas com veículos lentos (posto/logística).
    Cada detecção nova é associada ao track mais próximo por IoU.
    """

    def __init__(self, iou_min: float = 0.3, max_perdido: int = 15) -> None:
        self._iou_min = iou_min
        self._max_perdido = max_perdido
        self._tracks: dict[int, dict] = {}  # id → {bbox, conf, perdido}
        self._proximo_id: int = 1

    def update(
        self,
        dets_xywh: list[tuple[int, int, int, int, float]],
        _frame: np.ndarray,
    ) -> list[tuple[int, int, int, int, float, int]]:
        dets = [(x, y, x + w, y + h, c) for x, y, w, h, c in dets_xywh]
        matched, unmatched_dets, unmatched_tracks = self._match(dets)
        result: list[tuple[int, int, int, int, float, int]] = []

        for di, tid in matched:
            x1, y1, x2, y2, conf = dets[di]
            self._tracks[tid].update(bbox=(x1, y1, x2, y2), conf=conf, perdido=0)
            result.append((x1, y1, x2 - x1, y2 - y1, conf, tid))

        for di in unmatched_dets:
            x1, y1, x2, y2, conf = dets[di]
            tid = self._proximo_id
            self._proximo_id += 1
            self._tracks[tid] = {"bbox": (x1, y1, x2, y2), "conf": conf, "perdido": 0}
            result.append((x1, y1, x2 - x1, y2 - y1, conf, tid))

        mortos = []
        for tid in unmatched_tracks:
            self._tracks[tid]["perdido"] += 1
            if self._tracks[tid]["perdido"] > self._max_perdido:
                mortos.append(tid)
        for tid in mortos:
            del self._tracks[tid]

        return result

    def _iou(self, a: tuple, b: tuple) -> float:
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
        if inter == 0:
            return 0.0
        return inter / ((ax2-ax1)*(ay2-ay1) + (bx2-bx1)*(by2-by1) - inter)

    def _match(self, dets):
        if not self._tracks or not dets:
            return [], list(range(len(dets))), list(self._tracks.keys())
        track_ids = list(self._tracks.keys())
        pairs = sorted(
            [(self._iou(d[:4], self._tracks[tid]["bbox"]), di, tid)
             for di, d in enumerate(dets)
             for tid in track_ids],
            reverse=True,
        )
        used_d, used_t, matched = set(), set(), []
        for iou, di, tid in pairs:
            if iou >= self._iou_min and di not in used_d and tid not in used_t:
                matched.append((di, tid))
                used_d.add(di)
                used_t.add(tid)
        return (
            matched,
            [di for di in range(len(dets)) if di not in used_d],
            [tid for tid in track_ids if tid not in used_t],
        )


# ── Estado OCR por track ──────────────────────────────────────────────────────

class _EstadoTrack:
    """Estado OCR acumulado de um veículo rastreado."""

    __slots__ = ("track_id", "frames_visto", "ultimo_ocr_frame",
                 "resultados", "emitido", "bbox", "conf_det")

    def __init__(self, track_id: int) -> None:
        self.track_id = track_id
        self.frames_visto: int = 0
        self.ultimo_ocr_frame: int = -1
        self.resultados: list[tuple[str, str, float]] = []  # (placa, padrao, conf)
        self.emitido: bool = False
        self.bbox: tuple[int, int, int, int] | None = None
        self.conf_det: float = 0.0

    def precisa_ocr(self, frame_global: int, intervalo: int) -> bool:
        if self.emitido:
            return False
        if self.ultimo_ocr_frame < 0:
            return True
        return (frame_global - self.ultimo_ocr_frame) >= intervalo

    def registrar(self, placa: str, padrao: str, conf: float, frame_global: int) -> None:
        self.resultados.append((placa, padrao, conf))
        self.ultimo_ocr_frame = frame_global

    def placa_eleita(self, votos_min: int) -> tuple[str, str, float] | None:
        if len(self.resultados) < votos_min:
            return None
        contagem = Counter(p for p, _, _ in self.resultados)
        placa, n_votos = contagem.most_common(1)[0]
        if n_votos < votos_min:
            return None
        candidatos = [(c, pad) for p, pad, c in self.resultados if p == placa]
        melhor_conf, padrao = max(candidatos)
        return placa, padrao, melhor_conf


# ── Interface pública ─────────────────────────────────────────────────────────

class Tracker:
    """
    Tracker de veículos com fallback automático:
      - ByteTrack (boxmot)  se disponível
      - IoU interno         sempre disponível

    Uso:
        tracker = Tracker(ocr_a_cada_n_frames=5, votos_emitir=2)
        tracker.carregar()
        assert tracker.ativo()  # sempre True
    """

    def __init__(self, ocr_a_cada_n_frames: int = 5, votos_emitir: int = 2,
                 paciencia_frames: int = 40) -> None:
        self._ocr_intervalo = max(1, ocr_a_cada_n_frames)
        self._votos = max(1, votos_emitir)
        # Frames (de detecção, não frames brutos) tolerados sem match antes de considerar
        # o veículo perdido. Um valor baixo fragmenta o track de um veículo parado na
        # bomba (oclusão momentânea por pessoa/mangueira) em vários IDs — cada um vota do
        # zero e pode emitir uma placa levemente diferente pro mesmo carro.
        self._paciencia = max(1, paciencia_frames)
        self._backend = None       # instância do tracker (ByteTrack ou _IoUTracker)
        self._usando_bytetrack = False
        self._estados: dict[int, _EstadoTrack] = {}
        self._frame_count: int = 0

    def carregar(self) -> None:
        try:
            from boxmot import ByteTrack
            self._backend = ByteTrack(
                track_high_thresh=0.5,
                track_low_thresh=0.1,
                new_track_thresh=0.6,
                track_buffer=self._paciencia,
                match_thresh=0.8,
            )
            self._usando_bytetrack = True
            log.info(
                "ByteTrack (boxmot) ativo — OCR a cada %d frames, %d voto(s) para emitir, "
                "paciência %d frames",
                self._ocr_intervalo, self._votos, self._paciencia,
            )
        except Exception as e:
            log.info(
                "boxmot indisponível (%s) — usando tracker IoU interno "
                "(OCR a cada %d frames, %d voto(s) para emitir, paciência %d frames)",
                e, self._ocr_intervalo, self._votos, self._paciencia,
            )
            self._backend = _IoUTracker(iou_min=0.3, max_perdido=self._paciencia)
            self._usando_bytetrack = False

    def ativo(self) -> bool:
        return self._backend is not None

    @property
    def usando_bytetrack(self) -> bool:
        return self._usando_bytetrack

    # ── API principal ─────────────────────────────────────────────────────────

    def update(
        self,
        bboxes_xywh: list[tuple[int, int, int, int, float]],
        frame: np.ndarray,
    ) -> list[tuple[int, int, int, int, float, int]]:
        """
        Recebe lista de (x, y, w, h, conf) do YOLO.
        Devolve lista de (x, y, w, h, conf_det, track_id) para tracks ativos.
        """
        self._frame_count += 1

        if self._usando_bytetrack:
            return self._update_bytetrack(bboxes_xywh, frame)
        return self._update_iou(bboxes_xywh, frame)

    def _update_bytetrack(self, bboxes_xywh, frame):
        if not bboxes_xywh:
            self._backend.update(np.empty((0, 6), dtype=np.float32), frame)
            self._limpar_mortos(set())
            return []

        dets = np.array(
            [[x, y, x + w, y + h, c, 0.0] for x, y, w, h, c in bboxes_xywh],
            dtype=np.float32,
        )
        raw = self._backend.update(dets, frame)
        if raw is None or len(raw) == 0:
            self._limpar_mortos(set())
            return []

        ids_ativos: set[int] = set()
        saida: list[tuple[int, int, int, int, float, int]] = []
        for row in raw:
            x1, y1, x2, y2 = int(row[0]), int(row[1]), int(row[2]), int(row[3])
            tid = int(row[4])
            conf = float(row[5])
            ids_ativos.add(tid)
            self._registrar_track(tid, (x1, y1, x2, y2), conf)
            saida.append((x1, y1, x2 - x1, y2 - y1, conf, tid))

        self._limpar_mortos(ids_ativos)
        return saida

    def _update_iou(self, bboxes_xywh, frame):
        raw = self._backend.update(bboxes_xywh, frame)
        ids_ativos: set[int] = set()
        for x, y, w, h, conf, tid in raw:
            ids_ativos.add(tid)
            self._registrar_track(tid, (x, y, x + w, y + h), conf)
        self._limpar_mortos(ids_ativos)
        return raw

    def _registrar_track(self, tid: int, bbox: tuple, conf: float) -> None:
        if tid not in self._estados:
            self._estados[tid] = _EstadoTrack(tid)
            log.debug("Tracker: novo veículo ID=%d", tid)
        st = self._estados[tid]
        st.frames_visto += 1
        st.bbox = bbox
        st.conf_det = conf

    # ── OCR state management ──────────────────────────────────────────────────

    def precisa_ocr(self, track_id: int) -> bool:
        st = self._estados.get(track_id)
        return st.precisa_ocr(self._frame_count, self._ocr_intervalo) if st else False

    def registrar_ocr(self, track_id: int, placa: str, padrao: str, conf: float) -> None:
        st = self._estados.get(track_id)
        if st:
            st.registrar(placa, padrao, conf, self._frame_count)
            log.debug(
                "Tracker ID=%d: OCR=%s conf=%.2f (%d/%d votos)",
                track_id, placa, conf, len(st.resultados), self._votos,
            )

    def placa_pronta(self, track_id: int) -> tuple[str, str, float] | None:
        st = self._estados.get(track_id)
        if st is None or st.emitido:
            return None
        return st.placa_eleita(self._votos)

    def marcar_emitido(self, track_id: int) -> None:
        st = self._estados.get(track_id)
        if st:
            st.emitido = True

    def votos_atuais(self, track_id: int) -> int:
        st = self._estados.get(track_id)
        return len(st.resultados) if st else 0

    # ── Manutenção interna ────────────────────────────────────────────────────

    def _limpar_mortos(self, ids_ativos: set[int]) -> None:
        mortos = [tid for tid in self._estados if tid not in ids_ativos]
        for tid in mortos:
            st = self._estados.pop(tid)
            if st.resultados and not st.emitido:
                melhor = st.placa_eleita(1)
                if melhor:
                    log.debug(
                        "Tracker ID=%d saiu sem emitir (melhor: %s, %d leitura(s))",
                        tid, melhor[0], len(st.resultados),
                    )
