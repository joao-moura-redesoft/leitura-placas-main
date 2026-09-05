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
  - SUSPENDE o OCR do track que gastou `max_ocr_sem_leitura` tentativas sem produzir uma
    única leitura válida (ver `_EstadoTrack.contar_tentativa`) — é o perfil de texto de
    cena (letreiro, adesivo, texto de piso), que é caixa fixa e nunca sai do quadro
"""
from __future__ import annotations
import heapq
import itertools
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

# Contador global e monotônico de leituras de OCR registradas, de todos os tracks. Dá a
# `leituras_recentes` uma ordem TOTAL por recência — ver o corte lá. Não é timestamp de
# propósito: não depende de relógio de parede (que os testes mockam) e não empata.
_CONTADOR_LEITURA = itertools.count()


class _EstadoTrack:
    """Estado OCR acumulado de um veículo rastreado."""

    __slots__ = ("track_id", "frames_visto", "ultimo_ocr_frame",
                 "resultados", "emitido", "bbox", "conf_det", "frames_sem_match",
                 "tentativas_ocr", "desistiu", "_seqs")

    def __init__(self, track_id: int) -> None:
        self.track_id = track_id
        self.frames_visto: int = 0
        self.ultimo_ocr_frame: int = -1
        # Quantas vezes o ensemble foi autorizado a rodar neste track, TENHA validado ou
        # não. `resultados` só cresce quando `validar()` aceitou o texto, então ele não
        # mede custo: um track que nunca lê nada fica com `resultados` vazio para sempre
        # e é justamente o que gasta mais OCR. Ver `contar_tentativa`.
        self.tentativas_ocr: int = 0
        # OCR suspenso neste track por ter esgotado o teto sem uma leitura válida.
        self.desistiu: bool = False
        self.resultados: list[tuple[str, str, float]] = []  # (placa, padrao, conf)
        # Ordem GLOBAL de chegada de cada item de `resultados`, para `leituras_recentes`
        # poder cortar por recência entre tracks diferentes. Lista paralela em vez de uma
        # 4-tupla porque `resultados` é desempacotado como trio em vários pontos
        # (`placa_eleita`, `consenso`, `leituras_do_track`) e mudar a aridade quebraria
        # todos em silêncio.
        self._seqs: list[int] = []
        self.emitido: bool = False
        self.bbox: tuple[int, int, int, int] | None = None
        self.conf_det: float = 0.0
        # Frames de detecção seguidos em que este track não veio na saída do backend.
        # Zerado a cada reaparecimento; ver Tracker._limpar_mortos.
        self.frames_sem_match: int = 0

    def precisa_ocr(self, frame_global: int, intervalo: int) -> bool:
        if self.emitido or self.desistiu:
            return False
        if self.ultimo_ocr_frame < 0:
            return True
        return (frame_global - self.ultimo_ocr_frame) >= intervalo

    def contar_tentativa(self, max_sem_leitura: int) -> bool:
        """Conta uma tentativa de OCR; True se foi ESTA que esgotou o teto.

        O teto vale só para track que NUNCA produziu leitura válida (`resultados` vazio) —
        essa condição é a que separa o caso patológico do veículo difícil:

          - Texto de cena (letreiro, adesivo, texto de piso) nunca valida NADA. É uma caixa
            fixa no quadro, o tracker a mantém com o mesmo id indefinidamente, e a cada
            tentativa ela gasta as três passadas do ensemble. Medido no log de 04/09/2026,
            cam1: o trk16 sobre a palavra ENTRADA rodou `ENTRR6DA`/`ENNTFADA`/`ENTTRADA`
            hora após hora, e as três passadas custam ~62 ms cada rodada.
          - Veículo com placa difícil produz leitura de vez em quando. A PRIMEIRA leitura
            válida desarma o teto para sempre neste track, então um carro parado na bomba
            que já leu uma vez nunca é abandonado por ficar 30 s ocluso por uma pessoa.

        O que NÃO é feito aqui, de propósito: mexer no intervalo de OCR. `ultimo_ocr_frame`
        só é atualizado em `registrar`, que só roda quando `validar()` aceitou — então um
        track que não valida é reexaminado a CADA frame de detecção, e não a cada
        `ocr_a_cada_n_frames`. Isso é acidental (o intervalo não throttla justamente quem
        mais custa), mas também é o que dá muitas chances à placa difícil, e nunca foi
        medido: mudar junto tornaria impossível saber qual das duas coisas mexeu na taxa de
        leitura do posto.
        """
        self.tentativas_ocr += 1
        if (max_sem_leitura > 0 and not self.resultados
                and self.tentativas_ocr >= max_sem_leitura):
            self.desistiu = True
            return True
        return False

    def registrar(self, placa: str, padrao: str, conf: float, frame_global: int) -> None:
        self.resultados.append((placa, padrao, conf))
        self._seqs.append(next(_CONTADOR_LEITURA))
        self.ultimo_ocr_frame = frame_global

    def resultados_ordenados(self):
        """`(seq, placa, padrao, conf)` — `seq` é a ordem global de chegada da leitura."""
        return zip(self._seqs, *zip(*self.resultados)) if self.resultados else ()

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
                 paciencia_frames: int = 40, max_ocr_sem_leitura: int = 60) -> None:
        self._ocr_intervalo = max(1, ocr_a_cada_n_frames)
        self._votos = max(1, votos_emitir)
        # Teto de tentativas de OCR de um track que nunca produziu leitura válida
        # (0 = sem teto). Ver `_EstadoTrack.contar_tentativa`.
        self._max_sem_leitura = max(0, max_ocr_sem_leitura)
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
                "ByteTrack (boxmot) ativo: OCR a cada %d frames, %d voto(s) para emitir, "
                "paciência %d frames, teto de %s tentativa(s) sem leitura",
                self._ocr_intervalo, self._votos, self._paciencia,
                self._max_sem_leitura or "∞",
            )
        except Exception as e:
            log.info(
                "boxmot indisponível (%s), usando tracker IoU interno "
                "(OCR a cada %d frames, %d voto(s) para emitir, paciência %d frames, "
                "teto de %s tentativa(s) sem leitura)",
                e, self._ocr_intervalo, self._votos, self._paciencia,
                self._max_sem_leitura or "∞",
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
        """Autoriza (e CONTA) uma passada de OCR neste track.

        Contar aqui, e não em `registrar_ocr`, é o ponto todo: `registrar_ocr` só é chamado
        quando `validar()` aceitou o texto, então a tentativa que não validou — a que o teto
        existe para limitar — era invisível ao tracker. Quem pergunta roda: o pipeline usa a
        resposta imediatamente para chamar (ou não) o ensemble.
        """
        st = self._estados.get(track_id)
        if st is None or not st.precisa_ocr(self._frame_count, self._ocr_intervalo):
            return False
        if st.contar_tentativa(self._max_sem_leitura):
            log.info(
                "trk%d: OCR SUSPENSO após %d tentativas sem UMA leitura válida "
                "(tracker_max_ocr_sem_leitura=%d). Caixa fixa de texto de cena "
                "(letreiro, adesivo, texto de piso) fica exatamente assim, e cada "
                "tentativa custa as passadas do ensemble inteiro. A primeira leitura "
                "válida desarmaria o teto, mas não houve nenhuma.",
                track_id, st.tentativas_ocr, self._max_sem_leitura,
            )
            return False
        return True

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
                "Leitura %d de trk%d: %s conf=%.2f, líder %d/%d (emite com %d)",
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
        # Corta por RECENCIA, e nao pela ordem em que os tracks entraram no dict.
        #
        # `self._estados` e indexado por track_id, entao iterar por ele e iterar por ordem de
        # CRIACAO do track: o `[-limite:]` favorecia sistematicamente os ids mais altos. O
        # caso que a feature existe para salvar era justamente o que perdia — o carro parado
        # na bomba (track antigo, muitas leituras boas acumuladas) via as suas serem
        # descartadas em favor de um carro de fundo que acabou de aparecer.
        # (Auditoria 27/08/2026, achado M5.)
        #
        # `seq` e um contador monotonico por leitura registrada, e nao um timestamp: nao
        # depende de relogio de parede (que os testes mockam) e da ordem total mesmo entre
        # leituras do mesmo instante.
        leituras: list[tuple[int, str, float]] = []
        for st in self._estados.values():
            if st.emitido:
                continue
            leituras.extend((seq, p, c) for seq, p, _, c in st.resultados_ordenados())

        if not limite or limite <= 0:
            leituras.sort(key=lambda x: x[0])
            return [(p, c) for _, p, c in leituras]

        # `heapq.nlargest` evita ordenar a lista INTEIRA quando só as `limite` mais
        # recentes importam — esta função é chamada até ~13x por /api/leitura (ver
        # app/web/leitura.py::_leituras_do_continuo). `nlargest` devolve em ordem
        # DESCENDENTE de seq; `reversed` restaura a ordem ascendente (mais antiga
        # primeiro) que `sort()+[-limite:]` produzia, para não mudar o comportamento
        # observável de quem consome esta lista.
        maiores = heapq.nlargest(limite, leituras, key=lambda x: x[0])
        return [(p, c) for _, p, c in reversed(maiores)]

    @property
    def votos_minimos(self) -> int:
        """Votos exigidos para emitir (`tracker_votos_emitir`). Exposto porque o pipeline
        precisa dele para decidir se a leitura conta como confirmada — a mesma pergunta
        que `app/visao/consenso.py` faz nos dois caminhos de leitura."""
        return self._votos

    def leituras_do_track(self, track_id: int) -> list[tuple[str, float]]:
        """As leituras cruas `(placa, confianca)` deste veículo, ou lista vazia.

        Existe para o pipeline poder calcular `acordo` pela MESMA métrica da leitura
        reativa (`acordo_metrica`). Sem isto o contínuo só sabia contar string exata, e as
        duas origens gravavam escalas diferentes na mesma coluna `deteccoes.acordo`
        (auditoria 27/08/2026, achado A11).
        """
        st = self._estados.get(track_id)
        return [(p, c) for p, _, c in st.resultados] if st else []

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
                    # As leituras CRUAS vão na linha porque são elas que dizem qual das duas
                    # causas travou a emissão, e sem elas as duas ficam idênticas no log:
                    #
                    #   ['BZB8141','B2B8I41','BZB8T41'] → mesma placa, e o que barrou foi o
                    #       agrupamento/`votos_min` (`placa_eleita` com `votos_min=1` elege,
                    #       com 2 não: `agrupar_por_veiculo` partiu o pool e o maior grupo
                    #       ficou menor que o mínimo).
                    #   ['BZB8141','RZP0J47','B3D8T4I'] → o OCR não está lendo o veículo, e
                    #       nenhum limiar de consenso conserta isso.
                    #
                    # O caso real que motivou: 04/09/2026, cam1, "trk2 SAIU sem emitir — 3
                    # leitura(s), melhor BZB8141 com 1 voto(s)". Três leituras acumuladas,
                    # um veículo perdido, e a linha não permitia distinguir as duas coisas.
                    log.info(
                        "trk%d SAIU sem emitir: %d leitura(s) %s, melhor %s com %d "
                        "voto(s) (emite com %d)",
                        tid, total, [p for p, _, _ in st.resultados], melhor[0],
                        votos_lider, self._votos,
                    )
            elif not st.resultados:
                # Veículo detectado e nunca lido é um caso diferente de "lido e não
                # convergiu", e some do log se não for dito aqui — o de cima só dispara
                # quando houve alguma leitura válida.
                #
                # As tentativas vão na linha porque são o CUSTO desse track: é o número
                # que separa "passou pelo quadro e não deu tempo" (2, 3 tentativas) de
                # "caixa fixa consumindo o ensemble" (dezenas, e aí `desistiu` já entrou).
                log.info("trk%d SAIU sem nenhuma leitura válida: %d tentativa(s) de OCR%s",
                         tid, st.tentativas_ocr, " (OCR suspenso antes de sair)" if st.desistiu else "")
