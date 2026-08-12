"""Como as caixas do PaddleOCR viram uma placa — o ponto onde a moto se perdia.

Placa de moto tem DUAS linhas (letras em cima, dígitos embaixo) e o Paddle devolve uma
caixa por linha, de tamanho parecido. O código ficava só com a MAIOR caixa — regra certa
para carro (a placa é o maior texto do crop, 'BRASIL'/cidade são menores), destrutiva para
moto: jogava fora metade da placa, sempre. Medido nas 27 placas de moto de
`testes/dataset.json` antes da correção: 0/27, e em todas o retorno era uma linha sozinha.

Os testes usam um fake do `predict` do Paddle: o que está sob teste é a montagem do texto
a partir das caixas, não o reconhecimento — que já é medido pelo dataset.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.visao.ocr.engines import OCR


def _caixa(x0, y0, x1, y1):
    return np.array([x0, y0, x1, y1], dtype=float)


class PaddleFake:
    """Devolve caixas fixas no formato do PaddleOCR 3.x (rec_boxes [x1,y1,x2,y2])."""

    def __init__(self, itens):
        self.itens = itens          # [(texto, score, (x0,y0,x1,y1))]

    def predict(self, _img):
        return [{
            "rec_texts": [t for t, _s, _b in self.itens],
            "rec_scores": [s for _t, s, _b in self.itens],
            "rec_boxes": [_caixa(*b) for _t, _s, b in self.itens],
        }]


@pytest.fixture
def ocr():
    o = OCR(engine="paddleocr", deskew_ativo=False)
    return o


def _ler(ocr, itens, tamanho=(140, 200)):
    ocr._paddle = PaddleFake(itens)
    return ocr._ler_paddleocr(np.zeros((*tamanho, 3), dtype=np.uint8))


def test_moto_junta_as_duas_linhas_em_ordem_de_leitura(ocr):
    """Regressão do 0/27: 'YZA' + '3456' tem que virar 'YZA3456', não '3456'."""
    texto, _conf = _ler(ocr, [
        ("3456", 1.00, (10, 60, 190, 105)),     # dígitos embaixo (caixa MAIOR)
        ("YZA",  0.98, (15, 10, 185, 55)),      # letras em cima
    ])
    assert texto == "YZA3456"


def test_ordem_vem_da_posicao_e_nao_da_ordem_de_chegada(ocr):
    """O Paddle não garante ordem de leitura na lista; quem ordena é a geometria."""
    de_baixo_pra_cima = _ler(ocr, [
        ("4567", 1.0, (10, 60, 190, 105)),
        ("NOP",  1.0, (15, 10, 185, 55)),
    ])
    de_cima_pra_baixo = _ler(ocr, [
        ("NOP",  1.0, (15, 10, 185, 55)),
        ("4567", 1.0, (10, 60, 190, 105)),
    ])
    assert de_baixo_pra_cima[0] == de_cima_pra_baixo[0] == "NOP4567"


def test_descarta_cidade_e_uf_pelo_tamanho(ocr):
    """'RJ CIDADE'/'DETRAN' são bem menores que uma linha de placa — não entram.

    É o que a regra da maior caixa acertava e que não pode ser perdido ao juntar linhas.
    """
    texto, _conf = _ler(ocr, [
        ("RJCIDADE", 1.00, (40, 2, 160, 14)),   # faixa da cidade: área pequena
        ("YZA",      0.98, (15, 20, 185, 65)),
        ("3456",     1.00, (10, 70, 190, 115)),
    ])
    assert texto == "YZA3456"


def test_carro_com_caixa_unica_nao_muda(ocr):
    """Caminho do carro: uma caixa só, comportamento idêntico ao de antes."""
    texto, conf = _ler(ocr, [("QVH1067", 0.94, (5, 5, 395, 125))], tamanho=(130, 400))
    assert (texto, conf) == ("QVH1067", 0.94)


def test_confianca_e_a_da_pior_linha(ocr):
    """A placa só vale inteira: uma linha incerta não pode se esconder atrás da outra.

    Com média, 'HP'(0.50) + '2371'(1.00) sairia com 0.75 e passaria por leitura sólida —
    e é essa confiança que alimenta o consenso e o `acordo` gravado na detecção.
    """
    _texto, conf = _ler(ocr, [
        ("HP",   0.50, (15, 10, 185, 55)),
        ("2371", 1.00, (10, 60, 190, 105)),
    ])
    assert conf == 0.50


def test_sem_caixa_nenhuma_devolve_vazio(ocr):
    assert _ler(ocr, []) == ("", 0.0)


def test_ignora_caixa_sem_texto_aproveitavel(ocr):
    """Pontuação/ruído que o Paddle às vezes devolve não pode virar caractere de placa."""
    texto, _conf = _ler(ocr, [
        ("--",   0.9, (15, 10, 185, 55)),
        ("2371", 1.0, (10, 60, 190, 105)),
    ])
    assert texto == "2371"


def test_retry_sem_limpeza_mercosul_quando_ela_zera_a_leitura(monkeypatch, ocr):
    """Placa ANTIGA de moto confundida com Mercosul: a limpeza apaga o 1º char de cada
    linha (pinta os cantos esquerdos) e o OCR fica sem texto. Tem que tentar de novo sem ela.

    Aconteceu numa moto real do posto: Paddle ia de 'NOI'+'5947' para nada.
    """
    chamadas = []

    def falso_preproc(crop, limpar_mercosul=True):
        chamadas.append(limpar_mercosul)
        ocr._ultimo_limpou_mercosul = limpar_mercosul
        return crop

    monkeypatch.setattr(ocr, "_preprocessar_dl", falso_preproc)
    # com limpeza → nada; sem limpeza → lê
    ocr._paddle = PaddleFake([])
    saidas = iter([("", 0.0), ("YOI5947", 0.8)])
    monkeypatch.setattr(ocr, "_ler_paddleocr", lambda _img: next(saidas))

    assert ocr.ler(np.zeros((40, 50, 3), dtype=np.uint8)) == ("YOI5947", 0.8)
    assert chamadas == [True, False], "deveria repetir uma vez, sem a limpeza"


def test_sem_retry_quando_a_primeira_leitura_ja_deu_texto(monkeypatch, ocr):
    """O retry é para o caso que falhou — não pode virar custo de toda leitura."""
    chamadas = []

    def falso_preproc(crop, limpar_mercosul=True):
        chamadas.append(limpar_mercosul)
        ocr._ultimo_limpou_mercosul = True
        return crop

    monkeypatch.setattr(ocr, "_preprocessar_dl", falso_preproc)
    ocr._paddle = PaddleFake([])
    monkeypatch.setattr(ocr, "_ler_paddleocr", lambda _img: ("ABC1234", 0.9))

    assert ocr.ler(np.zeros((40, 50, 3), dtype=np.uint8)) == ("ABC1234", 0.9)
    assert chamadas == [True]
