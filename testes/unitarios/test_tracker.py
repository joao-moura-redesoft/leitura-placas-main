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
