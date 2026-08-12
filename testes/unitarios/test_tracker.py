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
