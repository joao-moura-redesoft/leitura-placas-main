"""`frame_ao_vivo` (app/web/leitura.py) — o provedor de frame usado por `ler_placa`
quando o pipeline contínuo já está com a câmera aberta.

Regressão que este arquivo protege: o ajuste de ambiente passou a publicar frame na
cadência de DETECÇÃO (`deteccao_fps_max`, tipicamente 5/s = 200ms) em vez da cadência
da CÂMERA (`camera_fps`, tipicamente 15/s = 66ms) — ver app/visao/pipeline.py:_loop.
`ler_placa` consulta este provider a cada ~150ms; sem a correção abaixo, boa parte das
consultas pegaria o MESMO objeto de frame da tentativa anterior. Duas "fotos" idênticas
do loop reject-retry concordam 100% entre si e disparam a parada antecipada por
consenso sem NENHUMA concordância entre frames de verdade (ver `_eleger_placa` em
app/visao/leitura.py) — e ainda gastam uma passada de YOLO+OCR à toa.
"""
from __future__ import annotations
import threading
import time

import numpy as np
import pytest

from app.core import estado
from app.visao import pipeline
from app.web import leitura as leitura_web


class _PipelineFalso:
    """Só o que `frame_ao_vivo` lê de uma instância de Pipeline de verdade."""

    def __init__(self, intervalo_deteccao=0.2):
        self._intervalo_deteccao = intervalo_deteccao


@pytest.fixture
def camera_id():
    cam_id = 987654
    yield cam_id
    pipeline._instancias.pop(cam_id, None)
    with estado.lock:
        estado.frames_cameras.pop(cam_id, None)
        estado.frames_cameras_limpos.pop(cam_id, None)
        estado.ultimo_frame_ts.pop(cam_id, None)


def _publicar(cam_id, frame):
    estado.registrar_frame_camera_limpo(cam_id, frame)
    estado.registrar_frame_camera(cam_id, frame)  # grava ultimo_frame_ts


def test_camera_fora_do_pipeline_devolve_none(camera_id):
    assert leitura_web.frame_ao_vivo(camera_id) is None


def test_primeira_chamada_devolve_o_frame_disponivel_sem_exigir_novidade(camera_id):
    """A primeira chamada não tem 'último frame' pra comparar — não pode ficar
    esperando um frame 'novo' que não existe ainda."""
    pipeline._instancias[camera_id] = _PipelineFalso()
    frame = np.zeros((2, 2, 3), dtype=np.uint8)
    _publicar(camera_id, frame)

    provider = leitura_web.frame_ao_vivo(camera_id)
    inicio = time.time()
    resultado = provider()
    assert resultado is frame
    assert time.time() - inicio < 0.05, "primeira chamada não deveria esperar nada"


class TestFrameRepetido:
    def test_segunda_chamada_sem_frame_novo_devolve_none_apos_o_teto(self, camera_id):
        """Nunca devolve o MESMO objeto duas vezes — isso é o que causava consenso
        falso no reject-retry loop de ler_placa."""
        pipeline._instancias[camera_id] = _PipelineFalso(intervalo_deteccao=0.1)
        frame = np.zeros((2, 2, 3), dtype=np.uint8)
        _publicar(camera_id, frame)
        provider = leitura_web.frame_ao_vivo(camera_id)
        assert provider() is frame   # 1ª chamada consome o único frame disponível

        inicio = time.time()
        resultado = provider()       # nada novo foi publicado
        decorrido = time.time() - inicio
        assert resultado is None
        # teto = 1.5 * intervalo_deteccao = 0.15s — generoso o bastante pra não ser flaky
        assert 0.1 <= decorrido <= 0.5

    def test_frame_novo_publicado_durante_a_espera_e_devolvido_assim_que_aparece(self, camera_id):
        pipeline._instancias[camera_id] = _PipelineFalso(intervalo_deteccao=0.2)
        frame1 = np.zeros((2, 2, 3), dtype=np.uint8)
        _publicar(camera_id, frame1)
        provider = leitura_web.frame_ao_vivo(camera_id)
        assert provider() is frame1

        frame2 = np.ones((2, 2, 3), dtype=np.uint8)

        def _publicar_depois():
            time.sleep(0.08)
            _publicar(camera_id, frame2)

        threading.Thread(target=_publicar_depois).start()
        inicio = time.time()
        resultado = provider()
        decorrido = time.time() - inicio
        assert resultado is frame2
        assert decorrido < 0.3   # bem abaixo do teto de 0.3s (1.5 * 0.2)

    def test_chamadas_sucessivas_nunca_repetem_objeto(self, camera_id):
        """Sequência de 5 frames publicados um a um — a cada chamada, ou vem o
        próximo objeto novo, ou None; nunca o mesmo de antes.

        Guarda os ARRAYS retornados (não `id()` isolado) e só compara identidade no
        fim, com todos ainda referenciados por `vistos` — comparar `id()` coletado ao
        longo do tempo é uma armadilha clássica: assim que um array intermediário
        perde a última referência, o alocador do CPython pode reaproveitar o mesmo
        endereço para o array seguinte, dando `id()` igual para objetos diferentes.
        """
        pipeline._instancias[camera_id] = _PipelineFalso(intervalo_deteccao=0.05)
        provider = leitura_web.frame_ao_vivo(camera_id)

        vistos = []
        for i in range(5):
            frame = np.full((2, 2, 3), i, dtype=np.uint8)
            _publicar(camera_id, frame)
            resultado = provider()
            if resultado is not None:
                vistos.append(resultado)   # mantém vivo — id() só é seguro assim

        ids = [id(v) for v in vistos]   # seguro: todos os `vistos` seguem referenciados
        assert len(ids) == len(set(ids)), "algum frame foi devolvido 2x"

    def test_segunda_chamada_sem_frame_novo_desiste_rapido_sem_herdar_o_warmup(self, camera_id):
        """A espera de até 20s (ESPERA_PRIMEIRO_FRAME_SEG) é só para a 1ª chamada
        (pipeline ainda aquecendo). Uma vez que já veio um frame, se a câmera parar
        de publicar (idade > FRAME_MAX_IDADE_SEG) a chamada seguinte tem que desistir
        rápido, não herdar os 20s de tolerância da primeira."""
        pipeline._instancias[camera_id] = _PipelineFalso(intervalo_deteccao=0.1)
        frame = np.zeros((2, 2, 3), dtype=np.uint8)
        _publicar(camera_id, frame)
        provider = leitura_web.frame_ao_vivo(camera_id)
        assert provider() is frame   # consome a 1ª chamada de verdade

        with estado.lock:
            estado.ultimo_frame_ts.pop(camera_id, None)   # simula câmera parando de publicar
        inicio = time.time()
        resultado = provider()
        assert resultado is None
        assert time.time() - inicio < 1.0, "não deveria herdar o timeout de warm-up"


def test_pipeline_removido_entre_chamadas_usa_teto_padrao_sem_quebrar(camera_id):
    """Se a câmera for removida bem no meio de uma leitura em andamento, a função não
    pode estourar (KeyError/AttributeError) — só perde a referência à cadência real."""
    pipeline._instancias[camera_id] = _PipelineFalso(intervalo_deteccao=0.1)
    frame = np.zeros((2, 2, 3), dtype=np.uint8)
    _publicar(camera_id, frame)
    provider = leitura_web.frame_ao_vivo(camera_id)
    assert provider() is frame

    pipeline._instancias.pop(camera_id, None)   # câmera removida no meio da leitura
    resultado = provider()   # não deve levantar
    assert resultado is None
