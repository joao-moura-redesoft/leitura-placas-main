"""PaddleOCR e REFORCO: a falha dele nao pode derrubar a leitura.

Existe por causa de um incidente de campo (02/09/2026, maquina levada para a feira). O
`libpaddle.pyd` nao carregou — o Visual C++ Redistributable tinha acabado de ser instalado
e ainda exigia reboot (o instalador devolveu 3010) — e o botao "Ler Placa" passou a
responder **HTTP 500**. No MESMO instante, o fast-plate-ocr lia a placa com confianca 0,95
e o pipeline continuo emitia normalmente: o sistema jogava fora uma leitura boa por causa
de um engine acessorio que nao subiu.

`ocr_leitura_paddle` e documentado como "reforco na leitura GET" (config.py) — reforco que
derruba tudo quando falta nao e reforco, e requisito escondido.
"""
from __future__ import annotations

import pytest

from app.visao.ocr.auto import AutoOCRPaddle


class _PaddleQuebrado:
    """Dublê do caso real: a carga estoura numa dependencia nativa."""

    def __init__(self):
        self.tentou = False

    def carregar(self):
        self.tentou = True
        raise ImportError("DLL load failed while importing libpaddle")


class _PaddleOk:
    def __init__(self):
        self.tentou = False

    def carregar(self):
        self.tentou = True


@pytest.fixture
def sem_pai(monkeypatch):
    """Neutraliza a classe base: o que se mede aqui e SO o ramo do Paddle.

    Carregar o pai de verdade puxaria os modelos ONNX do fast-plate-ocr, e a suite
    unitaria roda sem rede e sem GPU.
    """
    pai = AutoOCRPaddle.__mro__[1]
    monkeypatch.setattr(pai, "carregar", lambda self: None, raising=False)
    monkeypatch.setattr(pai, "_engines", lambda self: [("fast_plate_ocr", object())],
                        raising=False)


def _instancia(paddle):
    ocr = AutoOCRPaddle.__new__(AutoOCRPaddle)
    ocr._paddle = paddle
    return ocr


def test_falha_do_paddle_nao_levanta(sem_pai):
    """O caso do incidente: antes disto, esta chamada virava HTTP 500 na rota."""
    ocr = _instancia(_PaddleQuebrado())
    ocr.carregar()          # nao pode levantar
    assert ocr._paddle is None


def test_leitura_segue_com_os_outros_engines(sem_pai):
    """Degradar significa CONTINUAR VOTANDO com quem sobrou, nao ficar sem ninguem."""
    ocr = _instancia(_PaddleQuebrado())
    ocr.carregar()
    nomes = [n for n, _ in ocr._engines()]
    assert nomes == ["fast_plate_ocr"]
    assert "paddleocr" not in nomes


def test_paddle_saudavel_continua_votando(sem_pai):
    """A degradacao nao pode ter custado o caminho feliz."""
    ocr = _instancia(_PaddleOk())
    ocr.carregar()
    assert ocr._paddle is not None
    assert [n for n, _ in ocr._engines()] == ["fast_plate_ocr", "paddleocr"]


def test_nao_reinsiste_na_carga_que_falhou(sem_pai):
    """`_engines` roda a cada recorte. Se o objeto quebrado continuasse ali, cada leitura
    pagaria uma tentativa de carga nativa — e repetiria o erro no log sem parar."""
    quebrado = _PaddleQuebrado()
    ocr = _instancia(quebrado)
    ocr.carregar()
    for _ in range(5):
        assert [n for n, _ in ocr._engines()] == ["fast_plate_ocr"]
    assert ocr._paddle is None


def test_avisa_no_log(sem_pai, caplog):
    """Degradar em silencio esconderia uma queda de acuracia que ninguem explicaria."""
    import logging
    with caplog.at_level(logging.WARNING, logger="app.visao.ocr.auto"):
        _instancia(_PaddleQuebrado()).carregar()
    texto = caplog.text.lower()
    assert "paddleocr indisponivel" in texto
    # A mensagem tem de dizer o que ACONTECE agora, nao so que algo falhou.
    assert "sem ele" in texto
