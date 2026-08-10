"""Cache de JPEG por câmera em app/streaming/stream.py.

O pipeline publica frame novo a `deteccao_fps_max` (ex.: 5/s), mas cada gerador MJPEG
roda a `fps_max` (15/s) — sem cache, todo viewer reencodava o mesmo frame ~3x (e cada
viewer adicional multiplicava o custo por nada, já que o frame não tinha mudado).
`_jpeg_cacheado` reusa os bytes quando o frame é o MESMO OBJETO (identidade, não
conteúdo — ver docstring do módulo) e a qualidade pedida é igual.
"""
from __future__ import annotations
import threading

import numpy as np
import pytest

from app.streaming import stream


@pytest.fixture(autouse=True)
def _cache_limpo():
    """Isola os testes: o cache é um dict de módulo, compartilhado por padrão."""
    stream.limpar_cache()
    yield
    stream.limpar_cache()


def _frame():
    return np.zeros((40, 40, 3), dtype=np.uint8)


class _ContadorDeEncode:
    """Substitui cv2.imencode contando quantas vezes o encode de verdade roda."""

    def __init__(self, monkeypatch):
        self.chamadas = 0
        original = stream.cv2.imencode

        def _contando(*args, **kwargs):
            self.chamadas += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(stream.cv2, "imencode", _contando)


def test_mesmo_objeto_de_frame_reusa_o_encode(monkeypatch):
    contador = _ContadorDeEncode(monkeypatch)
    frame = _frame()
    b1 = stream._jpeg_cacheado("cam1", frame, 75)
    b2 = stream._jpeg_cacheado("cam1", frame, 75)
    b3 = stream._jpeg_cacheado("cam1", frame, 75)
    assert contador.chamadas == 1
    assert b1 == b2 == b3


def test_objeto_novo_com_conteudo_identico_reencoda():
    """Regra é IDENTIDADE, não conteúdo — dois arrays iguais mas objetos diferentes
    contam como frames diferentes. É uma decisão de custo (comparar pixel a pixel
    seria tão caro quanto reencodar) documentada no módulo, não um bug."""
    a, b = _frame(), _frame()
    assert a is not b and np.array_equal(a, b)
    jpg_a = stream._jpeg_cacheado("cam1", a, 75)
    jpg_b = stream._jpeg_cacheado("cam1", b, 75)
    assert jpg_a == jpg_b   # mesmo conteúdo -> bytes iguais


def test_qualidade_diferente_reencoda_e_nao_cresce(monkeypatch):
    contador = _ContadorDeEncode(monkeypatch)
    frame = _frame()
    stream._jpeg_cacheado("cam1", frame, 75)
    stream._jpeg_cacheado("cam1", frame, 90)
    assert contador.chamadas == 2
    assert len(stream._cache_jpeg) == 1, "a entrada deveria ser SUBSTITUÍDA, não somada"


def test_cameras_diferentes_nao_se_atrapalham(monkeypatch):
    contador = _ContadorDeEncode(monkeypatch)
    f1, f2 = _frame(), _frame()
    stream._jpeg_cacheado(1, f1, 75)
    stream._jpeg_cacheado(2, f2, 75)
    stream._jpeg_cacheado(1, f1, 75)
    stream._jpeg_cacheado(2, f2, 75)
    assert contador.chamadas == 2
    assert set(stream._cache_jpeg.keys()) == {1, 2}


def test_concorrencia_mesmo_frame_bytes_iguais_e_no_maximo_um_encode_por_thread(monkeypatch):
    """N threads pedindo o mesmo frame ao mesmo tempo (viewers concorrentes) nunca
    devem produzir bytes diferentes entre si, mesmo que a corrida faça mais de uma
    encodar antes da primeira publicar no cache."""
    contador = _ContadorDeEncode(monkeypatch)
    frame = _frame()
    resultados = []
    barreira = threading.Barrier(8)

    def _pedir():
        barreira.wait()
        resultados.append(stream._jpeg_cacheado("cam1", frame, 75))

    threads = [threading.Thread(target=_pedir) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r == resultados[0] for r in resultados)
    assert contador.chamadas <= 8   # nunca mais que uma tentativa por thread

    # Depois da corrida inicial, o cache converge: mais pedidos não geram novo encode.
    antes = contador.chamadas
    for _ in range(5):
        stream._jpeg_cacheado("cam1", frame, 75)
    assert contador.chamadas == antes


class TestDescarte:
    def test_descartar_cache_remove_so_a_chave_pedida(self):
        stream._jpeg_cacheado(1, _frame(), 75)
        stream._jpeg_cacheado(2, _frame(), 75)
        stream.descartar_cache(1)
        assert set(stream._cache_jpeg.keys()) == {2}

    def test_descartar_camera_inexistente_nao_leva_erro(self):
        stream.descartar_cache(999)  # não deve levantar

    def test_limpar_cache_esvazia_tudo(self):
        stream._jpeg_cacheado(1, _frame(), 75)
        stream._jpeg_cacheado(2, _frame(), 75)
        stream.limpar_cache()
        assert stream._cache_jpeg == {}


class TestIntegracaoComPipeline:
    """Regressão: sem descartar o cache junto da limpeza de frame em `estado`, uma
    câmera removida ficava com o último JPEG preso para sempre — e podia ser servido
    a um viewer que reconectasse antes do primeiro frame novo pós-reinício."""

    def test_parar_camera_descarta_o_cache_daquela_camera(self, monkeypatch):
        import app.visao.pipeline as pipeline_mod
        monkeypatch.setitem(pipeline_mod._instancias, 42, _PipelineFalso())
        stream._jpeg_cacheado(42, _frame(), 75)
        pipeline_mod.parar_camera(42)
        assert 42 not in stream._cache_jpeg

    def test_parar_todas_descarta_o_cache_inteiro(self, monkeypatch):
        import app.visao.pipeline as pipeline_mod
        monkeypatch.setitem(pipeline_mod._instancias, 1, _PipelineFalso())
        monkeypatch.setitem(pipeline_mod._instancias, 2, _PipelineFalso())
        stream._jpeg_cacheado(1, _frame(), 75)
        stream._jpeg_cacheado(2, _frame(), 75)
        pipeline_mod.parar_todas()
        assert stream._cache_jpeg == {}


class _PipelineFalso:
    """Dublê mínimo: só precisa responder a `.parar()` sem tocar em câmera de verdade."""

    def parar(self):
        pass
