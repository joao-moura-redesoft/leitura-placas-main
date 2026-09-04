"""Estado de OCR por veículo rastreado — o que decide quando uma placa é emitida.

O tracker acumula leituras de OCR por track e só emite quando junta `votos_emitir`
concordantes. O ponto sensível é a sobrevivência desse acumulado a uma oclusão: no posto,
pessoa, mangueira e reflexo tapam a placa por alguns frames o tempo todo, e é para isso
que existe `tracker_paciencia_frames`.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.visao.tracker import Tracker

FRAME = np.zeros((480, 640, 3), dtype=np.uint8)
BBOX = [(100, 100, 120, 40, 0.9)]


@pytest.fixture
def tracker():
    t = Tracker(ocr_a_cada_n_frames=1, votos_emitir=2, paciencia_frames=5)
    t.carregar()
    return t


def _ver_veiculo(t) -> int:
    """Um frame com o veículo visível; devolve o track_id."""
    return t.update(BBOX, FRAME)[0][5]


def test_votos_sobrevivem_a_oclusao_curta(tracker):
    """Regressão: `_limpar_mortos` descartava o estado no PRIMEIRO frame de ausência.

    Nem o ByteTrack nem o `_IoUTracker` devolvem um track sem match — os dois o guardam
    internamente e voltam a devolvê-lo com o mesmo id. Descartar o estado na primeira
    ausência zerava os votos a cada oclusão de um frame, fazendo `paciencia_frames=40`
    valer 1 na prática e impedindo que um veículo parado na bomba fechasse o consenso.
    """
    tid = _ver_veiculo(tracker)
    tracker.registrar_ocr(tid, "ABC1D23", "mercosul", 0.9)
    assert tracker.votos_atuais(tid) == 1

    tracker.update([], FRAME)          # oclusão de 1 frame
    assert tracker.votos_atuais(tid) == 1, "o voto acumulado não pode sumir numa oclusão"

    assert _ver_veiculo(tracker) == tid
    tracker.registrar_ocr(tid, "ABC1D23", "mercosul", 0.9)
    assert tracker.placa_pronta(tid) == ("ABC1D23", "mercosul", 0.9)


def test_estado_expira_depois_da_paciencia(tracker):
    """A paciência é um prazo, não memória infinita: passado o prazo o veículo é
    esquecido e um track novo na mesma posição vota do zero."""
    tid = _ver_veiculo(tracker)
    tracker.registrar_ocr(tid, "ABC1D23", "mercosul", 0.9)

    for _ in range(6):                 # paciencia_frames=5 → o 6º expira
        tracker.update([], FRAME)

    assert tracker.votos_atuais(tid) == 0


def test_veiculo_ja_emitido_nao_reemite_apos_oclusao(tracker):
    """A marca `emitido` vivia no mesmo estado descartado, então o mesmo veículo era
    emitido de novo depois de qualquer oclusão. O cooldown de `Pipeline._emitir`
    escondia isso no banco, mas o pipeline seguia refazendo OCR e reemitindo."""
    tid = _ver_veiculo(tracker)
    for _ in range(2):
        tracker.registrar_ocr(tid, "ABC1D23", "mercosul", 0.9)
    assert tracker.placa_pronta(tid) is not None
    tracker.marcar_emitido(tid)

    tracker.update([], FRAME)
    assert _ver_veiculo(tracker) == tid
    assert tracker.placa_pronta(tid) is None, "não pode emitir o mesmo veículo duas vezes"


class TestLeiturasRecentes:
    """Achado C2 do review de 28/08/2026: `leituras_recentes` ordenava a lista INTEIRA de
    leituras a cada chamada só para pegar a cauda — trocado por `heapq.nlargest`. O
    comportamento observável (quais entram, em que ordem saem) tem de continuar igual."""

    BBOX2 = [(400, 100, 120, 40, 0.9)]  # bem longe de BBOX: vira um segundo track

    def test_corta_pelas_mais_recentes_em_ordem_ascendente(self, tracker):
        tid1, tid2 = [d[5] for d in tracker.update(BBOX + self.BBOX2, FRAME)]

        tracker.registrar_ocr(tid1, "AAA1111", "antigo", 0.9)   # seq 1 (mais antiga)
        tracker.registrar_ocr(tid2, "BBB2222", "antigo", 0.8)   # seq 2
        tracker.registrar_ocr(tid1, "CCC3333", "antigo", 0.7)   # seq 3 (mais nova)

        assert tracker.leituras_recentes(limite=2) == [("BBB2222", 0.8), ("CCC3333", 0.7)]

    def test_limite_maior_que_o_total_devolve_tudo_em_ordem(self, tracker):
        tid1, tid2 = [d[5] for d in tracker.update(BBOX + self.BBOX2, FRAME)]
        tracker.registrar_ocr(tid1, "AAA1111", "antigo", 0.9)
        tracker.registrar_ocr(tid2, "BBB2222", "antigo", 0.8)

        assert tracker.leituras_recentes(limite=12) == [("AAA1111", 0.9), ("BBB2222", 0.8)]

    def test_ja_emitido_fica_de_fora(self, tracker):
        tid1, tid2 = [d[5] for d in tracker.update(BBOX + self.BBOX2, FRAME)]
        tracker.registrar_ocr(tid1, "AAA1111", "antigo", 0.9)
        tracker.registrar_ocr(tid2, "BBB2222", "antigo", 0.8)
        tracker.marcar_emitido(tid1)

        assert tracker.leituras_recentes(limite=12) == [("BBB2222", 0.8)]


class TestFusaoPorPosicao:
    """O tracker votava por STRING EXATA, e por isso moto nunca era emitida.

    Em 24/08/2026, no bico 3 do ALTIPLANO, o trk1 leu a MESMA moto três vezes e as três
    strings saíram diferentes — `RLT2477`, `NLX2A77`, `RLX2A77`. Com `Counter` sobre a placa
    inteira, cada uma valia 1 voto, nenhuma chegava aos 2 exigidos, e o veículo saía do
    quadro sem emitir nada: "trk1 SAIU sem emitir — 3 leitura(s), melhor RLT2477 com
    1 voto(s)". As três, votadas posição a posição, dão `RLX2A77` — que era a placa certa e
    também a leitura de maior confiança do lote.
    """

    def test_tres_leituras_divergentes_da_mesma_moto_convergem(self, tracker):
        tid = _ver_veiculo(tracker)
        for placa, conf in [("RLT2477", 0.92), ("NLX2A77", 0.86), ("RLX2A77", 0.90)]:
            tracker.registrar_ocr(tid, placa, "mercosul", conf)

        pronto = tracker.placa_pronta(tid)
        assert pronto is not None, "três leituras da mesma moto e nada emitido"
        assert pronto[0] == "RLX2A77"

    def test_acordo_gravado_e_sobre_a_placa_emitida(self, tracker):
        """Com fusão, a emitida pode não ser nenhuma das lidas — o acordo tem de saber disso.

        Sem passar a placa, `consenso()` devolveria a contagem da string MAIS VOTADA, e o
        histórico gravaria em `deteccoes.acordo` um número referente a outra placa.
        """
        tid = _ver_veiculo(tracker)
        for placa in ["RLT2477", "NLX2A77", "RLX2A77"]:
            tracker.registrar_ocr(tid, placa, "mercosul", 0.9)

        placa = tracker.placa_pronta(tid)[0]
        votos, total = tracker.consenso(tid, placa)
        assert total == 3
        assert votos == 1, "só uma das três leituras era exatamente a placa emitida"
        # Acordo baixo com placa certa é o resultado desejado: emite e marca "a conferir",
        # em vez de não emitir nada (antes) ou emitir com falsa certeza.
        assert votos / total < 0.8

    def test_leituras_repetidas_continuam_com_acordo_alto(self, tracker):
        """A fusão não pode rebaixar o caso fácil, que é a maioria das leituras de carro."""
        tid = _ver_veiculo(tracker)
        for _ in range(3):
            tracker.registrar_ocr(tid, "ABC1D23", "mercosul", 0.9)

        placa = tracker.placa_pronta(tid)[0]
        assert placa == "ABC1D23"
        votos, total = tracker.consenso(tid, placa)
        assert (votos, total) == (3, 3)

    def test_dois_veiculos_no_mesmo_track_nao_geram_placa_inventada(self, tracker):
        """Tracker que troca a identidade da caixa acumula leitura de veículos diferentes."""
        tid = _ver_veiculo(tracker)
        for placa, conf in [("OSL2G55", 0.85), ("OSL2G55", 0.84), ("FWX9760", 0.74)]:
            tracker.registrar_ocr(tid, placa, "antigo", conf)

        pronto = tracker.placa_pronta(tid)
        assert pronto is not None
        assert pronto[0] == "OSL2G55", "o grupo majoritário tem de sobreviver intacto"


class TestOcrSuspensoSemLeitura:
    """Teto de tentativas para o track que nunca produz leitura válida.

    O caso real (log de 04/09/2026, cam1): a palavra ENTRADA pintada na cena entrou como
    track e o tracker a manteve indefinidamente — caixa fixa, nunca sai do quadro. Cada
    tentativa rodava o ensemble inteiro para devolver `ENTRR6DA`/`ENNTFADA`/`ENTTRADA`, que
    o validador recusa. Nada chegava ao banco; o custo era CPU e log, sem teto.

    O contrapeso, e o motivo de o teto olhar `resultados` em vez de contar tentativas
    seguidas: veículo com placa difícil LÊ de vez em quando, e a primeira leitura válida tem
    de desarmar o teto para sempre naquele track.
    """

    def _tracker(self, teto: int):
        t = Tracker(ocr_a_cada_n_frames=1, votos_emitir=2, paciencia_frames=5,
                    max_ocr_sem_leitura=teto)
        t.carregar()
        return t

    def test_para_de_gastar_ocr_depois_do_teto(self):
        t = self._tracker(3)
        tid = _ver_veiculo(t)

        assert t.precisa_ocr(tid)          # tentativa 1
        assert t.precisa_ocr(tid)          # tentativa 2
        assert not t.precisa_ocr(tid), "a 3ª tentativa esgota o teto e suspende o OCR"

        for _ in range(5):                 # e não volta em frame nenhum
            t.update(BBOX, FRAME)
            assert not t.precisa_ocr(tid)

    def test_uma_leitura_valida_desarma_o_teto(self):
        """Sem isto, um carro parado na bomba com a placa ocluída por uma pessoa seria
        abandonado no meio do abastecimento — o oposto do que `paciencia_frames` existe
        para garantir."""
        t = self._tracker(3)
        tid = _ver_veiculo(t)

        assert t.precisa_ocr(tid)
        t.registrar_ocr(tid, "ABC1D23", "mercosul", 0.9)

        for _ in range(20):
            t.update(BBOX, FRAME)
            assert t.precisa_ocr(tid), "track que já leu não pode ser abandonado"

    def test_teto_zero_desliga_a_regra(self):
        """`tracker_max_ocr_sem_leitura=0` restaura o comportamento anterior à regra."""
        t = self._tracker(0)
        tid = _ver_veiculo(t)
        for _ in range(30):
            assert t.precisa_ocr(tid)

    def test_emitido_continua_valendo_mais_que_o_teto(self):
        """A ordem dos dois cortes importa: um track já emitido não pode voltar a gastar
        tentativa só porque o teto ainda não foi alcançado."""
        t = self._tracker(0)
        tid = _ver_veiculo(t)
        for _ in range(2):
            t.registrar_ocr(tid, "ABC1D23", "mercosul", 0.9)
        t.marcar_emitido(tid)
        assert not t.precisa_ocr(tid)
