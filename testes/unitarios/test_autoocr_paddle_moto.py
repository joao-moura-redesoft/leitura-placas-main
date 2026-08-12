"""Arbitragem do AutoOCRPaddle — quem decide a placa quando os engines discordam.

Até 12/08/2026 a classe DESCARTAVA a leitura do PaddleOCR sempre que o crop era moto,
justificando que ele "não lê layout empilhado". A premissa era falsa: o Paddle lê as duas
linhas e devolve uma caixa para cada — quem jogava metade fora era `OCR._ler_paddleocr`,
que ficava só com a maior. Corrigido aquele defeito, a ordem se inverte nas 27 motos de
`testes/dataset.json`: PaddleOCR 22/27 contra 2/27 do fast_plate_ocr, que é o engine em que
o AutoOCR se apoia nesse layout.

Estes testes fixam a arbitragem com engines falsos — o reconhecimento em si é medido pelo
dataset, não aqui. O ambiente de desenvolvimento não consegue rodar AutoOCRPaddle de
verdade (easyocr + paddle no mesmo processo derrubam o interpretador), então esta é a
cobertura possível para a decisão.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.visao.ocr.auto import AutoOCRPaddle

CROP = np.zeros((140, 200, 3), dtype=np.uint8)


class PaddleFalso:
    def __init__(self, saida):
        self.saida = saida
        self.chamadas = 0

    def carregar(self):
        pass

    def ler(self, _crop):
        self.chamadas += 1
        return self.saida


@pytest.fixture
def ocr(monkeypatch):
    o = AutoOCRPaddle.__new__(AutoOCRPaddle)      # sem carregar modelos de verdade
    o._limiar_nitidez = 3500.0
    o.engine = "auto"
    o._ultimo_detalhe = {}
    return o


def _preparar(ocr, monkeypatch, *, auto, paddle, e_moto, hint="", nitido=True):
    """Fixa o que cada engine devolve e se o crop conta como moto/nítido."""
    def falso_auto(_self, _crop):
        _self._ultimo_e_moto = e_moto
        _self._ultimo_formato_hint = hint
        return {"placa": auto, "padrao": "antigo" if auto else None,
                "confianca": 0.9 if auto else 0.0, "votos": 1, "total_engines": 2,
                "detalhes": [{"engine": "easyocr", "placa": auto}]}

    monkeypatch.setattr("app.visao.ocr.auto.AutoOCR.ler_detalhado", falso_auto)
    ocr._paddle = PaddleFalso(paddle)
    # nitidez do crop preto é 0 → borrado; para "nítido" baixamos o limiar
    ocr._limiar_nitidez = -1.0 if nitido else 3500.0
    return ocr


def test_moto_usa_a_leitura_do_paddle(ocr, monkeypatch):
    """Regressão: antes o resultado do Paddle era descartado por ser moto."""
    _preparar(ocr, monkeypatch, auto="ABC1234", paddle=("YOI5947", 0.8), e_moto=True)

    d = ocr.ler_detalhado(CROP)

    assert d["placa"] == "YOI5947"
    assert ocr._paddle.chamadas == 1


def test_moto_borrada_tambem_usa_o_paddle(ocr, monkeypatch):
    """O outro ramo (crop borrado) tinha o mesmo descarte."""
    _preparar(ocr, monkeypatch, auto="ABC1234", paddle=("YOI5947", 0.8),
              e_moto=True, nitido=False)

    assert ocr.ler_detalhado(CROP)["placa"] == "YOI5947"


def test_moto_mantem_o_autoocr_quando_o_paddle_nao_valida(ocr, monkeypatch):
    """O Paddle ganha prioridade, não exclusividade — lixo dele não derruba leitura boa."""
    _preparar(ocr, monkeypatch, auto="ABC1234", paddle=("XX", 0.9), e_moto=True)

    assert ocr.ler_detalhado(CROP)["placa"] == "ABC1234"


def test_carro_nitido_nem_chama_o_paddle(ocr, monkeypatch):
    """Caminho do carro inalterado: AutoOCR validou em crop nítido, Paddle não roda.

    Importa por latência — o Paddle custa segundos por crop em CPU.
    """
    _preparar(ocr, monkeypatch, auto="QVH1067", paddle=("OUTRA12", 0.99), e_moto=False)

    d = ocr.ler_detalhado(CROP)

    assert d["placa"] == "QVH1067"
    assert ocr._paddle.chamadas == 0


def test_carro_nitido_sem_leitura_ainda_cai_no_paddle(ocr, monkeypatch):
    _preparar(ocr, monkeypatch, auto=None, paddle=("QVH1067", 0.95), e_moto=False)

    assert ocr.ler_detalhado(CROP)["placa"] == "QVH1067"


def test_hint_de_formato_chega_ao_paddle(ocr, monkeypatch):
    """Sem o hint, o Paddle era validado 'cru' e perdia a correção de posição do Mercosul.

    Em moto Mercosul a posição 5 é LETRA: 'FBI0123' tem que virar 'FBI0I23'.
    """
    _preparar(ocr, monkeypatch, auto=None, paddle=("FBI0123", 0.9),
              e_moto=True, hint="mercosul_moto")

    d = ocr.ler_detalhado(CROP)

    assert d["placa"] == "FBI0I23"
    assert d["padrao"] == "mercosul"


def test_concordancia_em_moto_nao_duplica_o_detalhe(ocr, monkeypatch):
    """Quando os dois leem igual, não faz sentido registrar o paddle como voto extra."""
    _preparar(ocr, monkeypatch, auto="YOI5947", paddle=("YOI5947", 0.8), e_moto=True)

    d = ocr.ler_detalhado(CROP)

    assert d["placa"] == "YOI5947"
    assert [x["engine"] for x in d["detalhes"]] == ["easyocr"]
