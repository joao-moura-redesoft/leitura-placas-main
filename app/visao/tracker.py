"""Rastreador de veículos entre frames para reduzir chamadas OCR.

Implementação em duas camadas:
  1. ByteTrack (boxmot)  — se instalado: tracker robusto com Kalman filter
  2. _IoUTracker         — fallback interno puro Python: matching por IoU simples

Em ambos os casos tracker.ativo() retorna True e a interface é idêntica.
O pipeline usa `tracker=None` apenas quando tracker_ativo=nao no config.

Redução de OCR:
  - OCR roda no primeiro frame de cada track (novo veículo)
  - OCR roda novamente a cada `ocr_a_cada_n_frames` frames do mesmo track
  - Emite quando o track acumula `votos_emitir` leituras que CONVERGEM (ver
    `_EstadoTrack.placa_eleita`) — concordância por posição, não por string exata
"""
from __future__ import annotations
import logging
from collections import Counter

import numpy as np

from app.visao.consenso import (
    agrupar_por_veiculo, consenso_caractere, leitura_real_proxima, prior_de_formato,
)
from app.visao.validador import validar

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
                 "resultados", "emitido", "bbox", "conf_det", "frames_sem_match")

    def __init__(self, track_id: int) -> None:
        self.track_id = track_id
        self.frames_visto: int = 0
        self.ultimo_ocr_frame: int = -1
        self.resultados: list[tuple[str, str, float]] = []  # (placa, padrao, conf)
        self.emitido: bool = False
        self.bbox: tuple[int, int, int, int] | None = None
        self.conf_det: float = 0.0
        # Frames de detecção seguidos em que este track não veio na saída do backend.
        # Zerado a cada reaparecimento; ver Tracker._limpar_mortos.
        self.frames_sem_match: int = 0

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
        """A placa deste veículo, por consenso POR POSIÇÃO entre as leituras acumuladas.

        Votava por STRING EXATA (`Counter` sobre a placa inteira) até 25/08/2026, e era isso
        que fazia moto não ser emitida nunca: em 24/08/2026, no bico 3 do ALTIPLANO, o trk1
        leu a MESMA moto três vezes — `RLT2477`, `NLX2A77`, `RLX2A77` —, nenhuma string
        repetiu, o contador deu 1 voto para cada e o veículo saiu do quadro sem emitir
        ("trk1 SAIU sem emitir — 3 leitura(s), melhor RLT2477 com 1 voto(s)"). Votando
        posição a posição essas três leituras dão `RLX2A77`, que era a placa certa E a
        leitura de maior confiança do lote.

        `votos_min` continua sendo sobre a QUANTIDADE de leituras acumuladas, não sobre
        quantas bateram exatamente: exigir N leituras idênticas era a regra que travava, e
        trocar o critério sem trocar o contador só mudaria de lugar o mesmo bloqueio.
        """
        if len(self.resultados) < votos_min:
            return None

        leituras = [(p, c) for p, _, c in self.resultados]
        # Um track pode acumular leitura de VEÍCULOS diferentes quando o tracker troca a
        # identidade da caixa. Fundir tudo junto inventaria uma placa que ninguém leu — as
        # duas trancas de `visao.consenso` são as mesmas usadas na leitura reativa.
        grupos = agrupar_por_veiculo(leituras)
        pool = grupos[0] if grupos else leituras
        if len(pool) < votos_min:
            return None

        fundida = consenso_caractere(pool, formato=prior_de_formato(pool))

        if fundida and leitura_real_proxima(fundida, pool):
            v = validar(fundida)
            if v:
                # Confiança da MELHOR leitura que sustenta a placa fundida — mantém a
                # escala de `conf` que o pipeline já grava, que era a do vencedor.
                apoio = [c for q, c in pool if q == fundida] or [c for _, c in pool]
                return v[0], v[1], max(apoio)

        # Sem fusão válida: volta ao voto por string exata, que ainda é o certo quando as
        # leituras de fato se repetem.
        contagem = Counter(q for q, _ in pool)
        placa, n_votos = contagem.most_common(1)[0]
        if n_votos < votos_min:
            return None
        candidatos = [(c, pad) for p, pad, c in self.resultados if p == placa]
        if not candidatos:
            return None
        melhor_conf, padrao = max(candidatos)
        return placa, padrao, melhor_conf

    def consenso(self, placa: str | None = None) -> tuple[int, int]:
        """(leituras que apontaram `placa`, total de leituras OCR deste veículo).

        A razão entre os dois é o `acordo` do contínuo no modo tracker, e é diretamente
        comparável ao da leitura reativa: nos dois casos é "que fração das leituras
        INDEPENDENTES do mesmo veículo apontou a placa que foi emitida".

        `placa` PRECISA ser passada por quem vai gravar o número, e passou a existir junto
        com a fusão por posição em `placa_eleita`. Antes daquela mudança a placa emitida era
        sempre a string mais votada, então contar "a mais votada" respondia a pergunta certa
        por coincidência. Com fusão a emitida pode não ser nenhuma das strings lidas (é o
        objetivo: `RLT2477` + `NLX2A77` + `RLX2A77` dão `RLX2A77`), e contar a mais votada
        passaria a gravar em `deteccoes.acordo` um número sobre OUTRA placa que não a
        emitida — exatamente o tipo de número que parece medida e não é.

        Sem `placa`, mantém o comportamento antigo (a mais votada). Esse caminho serve para
        LOG, onde a pergunta é "quão dispersas estão as leituras", não "quantas apoiam X".

        (0, 0) quando ainda não houve nenhuma leitura: quem chama nunca deve dividir por
        `total` sem checar, porque um veículo detectado e nunca lido tem consenso NENHUM,
        não consenso perfeito.
        """
        if not self.resultados:
            return 0, 0
        total = len(self.resultados)
        if placa is not None:
            return sum(1 for p, _, _ in self.resultados if p == placa), total
        contagem = Counter(p for p, _, _ in self.resultados)
        return contagem.most_common(1)[0][1], total


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
            log.info("Novo veículo trk%d", tid)
        st = self._estados[tid]
        st.frames_visto += 1
        st.frames_sem_match = 0
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
            # `(12/2 votos)` — o formato antigo — lia-se como "12 de 2" e, pior, escondia
            # o que decide a emissão: quantas leituras apontam a MESMA placa. No log de
            # 13/08/2026 o trk360 chegou a 20 leituras todas diferentes (SOB4318,
            # IID4318, IOB4318, SLD4318…) e a linha do tracker seguia parecendo progresso
            # rumo ao consenso. Aqui vai a contagem da placa líder, que é a que conta.
            votos_lider, total = st.consenso()
            log.info(
                "Leitura %d de trk%d: %s conf=%.2f — líder %d/%d (emite com %d)",
                total, track_id, placa, conf, votos_lider, total, self._votos,
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

    def leituras_recentes(self, limite: int = 12) -> list[tuple[str, float]]:
        """`(placa, conf)` das leituras acumuladas nos tracks AINDA VIVOS desta camera.

        Serve para a leitura reativa do GET aproveitar o que o monitoramento continuo ja
        leu do mesmo veiculo, em vez de comecar do zero. Sem isto o "Ler Placa" descartava
        evidencia melhor do que a que ele mesmo conseguia colher: em 24/08/2026 (bico 3 do
        ALTIPLANO) o tracker havia lido `RLX2A77` com confianca 0,96 e todos os char_probs
        >= 0,93 SETE SEGUNDOS antes da chamada, e o GET - que sondou apenas 2 dos 12 frames
        do seu orcamento antes de estourar o timeout - emitiu `HDX2477`.

        Traz TODAS as leituras dos tracks vivos e nao so as do "melhor" track: com o
        veiculo parado na bomba o tracker frequentemente reabre o mesmo carro como track
        novo, e escolher um deixaria de fora leituras do mesmo veiculo. Misturar dois
        veiculos aqui e seguro porque quem consome agrupa antes de fundir
        (`consenso.agrupar_por_veiculo`).

        Ja emitido fica de FORA: aquela leitura virou linha no historico, e reinjeta-la
        faria a chamada do bico votar em cima de um evento ja fechado.
        """
        leituras: list[tuple[str, float]] = []
        for st in self._estados.values():
            if st.emitido:
                continue
            leituras.extend((p, c) for p, _, c in st.resultados)
        return leituras[-limite:] if limite and limite > 0 else leituras

    @property
    def votos_minimos(self) -> int:
        """Votos exigidos para emitir (`tracker_votos_emitir`). Exposto porque o pipeline
        precisa dele para decidir se a leitura conta como confirmada — a mesma pergunta
        que `app/visao/consenso.py` faz nos dois caminhos de leitura."""
        return self._votos

    def consenso(self, track_id: int, placa: str | None = None) -> tuple[int, int]:
        """(votos da placa eleita, total de leituras) do track, ou (0, 0) se não existe.

        Ler ANTES de `marcar_emitido` não é obrigatório — a marca não apaga `resultados` —
        mas ler antes de `_limpar_mortos` é: o estado do veículo some quando ele deixa
        de aparecer por `paciencia_frames`.
        """
        st = self._estados.get(track_id)
        return st.consenso(placa) if st else (0, 0)

    # ── Manutenção interna ────────────────────────────────────────────────────

    def _limpar_mortos(self, ids_ativos: set[int]) -> None:
        """Expira o estado OCR de tracks ausentes — só depois de `paciencia` frames.

        Nem o ByteTrack nem o `_IoUTracker` devolvem um track que ficou sem match: os
        dois o guardam INTERNAMENTE (track_buffer / max_perdido) e voltam a devolvê-lo,
        com o MESMO id, quando o veículo reaparece. Descartar o `_EstadoTrack` no
        primeiro frame de ausência — como era feito aqui — zerava os votos de OCR e a
        marca `emitido` a cada oclusão de um único frame (pessoa passando na frente,
        mangueira, reflexo), fazendo `tracker_paciencia_frames` valer 1 na prática
        justamente contra o cenário que ele existe para cobrir. O contador espelha a
        paciência do backend, então os dois esquecem o veículo no mesmo momento.
        """
        for tid in list(self._estados):
            if tid in ids_ativos:
                continue
            st = self._estados[tid]
            st.frames_sem_match += 1
            if st.frames_sem_match <= self._paciencia:
                continue
            del self._estados[tid]
            if st.resultados and not st.emitido:
                melhor = st.placa_eleita(1)
                if melhor:
                    votos_lider, total = st.consenso()
                    log.info(
                        "trk%d SAIU sem emitir — %d leitura(s), melhor %s com %d voto(s)",
                        tid, total, melhor[0], votos_lider,
                    )
            elif not st.resultados:
                # Veículo detectado e nunca lido é um caso diferente de "lido e não
                # convergiu", e some do log se não for dito aqui — o de cima só dispara
                # quando houve alguma leitura válida.
                log.info("trk%d SAIU sem nenhuma leitura válida", tid)
