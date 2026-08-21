"""Regressão: a tela da câmera ficava em branco, sem erro nenhum.

`ao_vivo` era `camera_id in pipeline._instancias` — presença de um OBJETO Pipeline,
não de imagem saindo. Com `deteccao_automatica=nao` (o modo do servidor central) o
pipeline nem abre a câmera: `_loop` só dorme e nenhum frame é publicado. Mesmo assim
a instância fica registrada, então a tela dava `ao_vivo=true`, apontava o <img> para
/stream/{id}.mjpg — e o gerador girava para sempre sem emitir byte nenhum, porque só
emite quando há frame. Resultado: resposta 200 pendurada, o <img> nunca dispara `load`
nem `error`, retângulo vazio e silêncio. O mesmo acontecia quando `iniciar()` falhava
(a instância é mantida de propósito, para o supervisor tentar de novo).
"""
from __future__ import annotations
import time

import numpy as np
import pytest

import app.visao.pipeline as pipeline
from app.core import estado
from app.streaming import stream

CAM = 4242


class _PipelineFalso:
    """Só o que `estado_stream` lê de um Pipeline."""

    def __init__(self, *, deteccao_automatica=True, iniciando=False, thread_viva=True):
        self.deteccao_automatica = deteccao_automatica
        self.iniciando = iniciando
        self._thread = _ThreadFalsa(thread_viva) if thread_viva is not None else None

    def parar(self):
        pass


class _ThreadFalsa:
    def __init__(self, viva):
        self._viva = viva

    def is_alive(self):
        return self._viva


@pytest.fixture(autouse=True)
def _limpo():
    yield
    pipeline._instancias.pop(CAM, None)
    estado.esquecer_camera(CAM)
    stream.descartar_cache(CAM)


def _publicar_frame():
    estado.registrar_frame_camera(CAM, np.zeros((16, 16, 3), dtype=np.uint8))


class TestEstadoStream:
    def test_sem_pipeline_e_sob_demanda(self):
        assert pipeline.estado_stream(CAM) == "sob_demanda"

    def test_pipeline_em_modo_manual_nao_e_ao_vivo(self):
        """O caso que quebrava: `deteccao_automatica=nao` publica zero frames."""
        pipeline._instancias[CAM] = _PipelineFalso(deteccao_automatica=False)
        assert pipeline.estado_stream(CAM) == "sob_demanda"

    def test_pipeline_com_frame_recente_e_ao_vivo(self):
        pipeline._instancias[CAM] = _PipelineFalso()
        _publicar_frame()
        assert pipeline.estado_stream(CAM) == "ao_vivo"

    def test_pipeline_subindo_ainda_sem_frame_e_aquecendo(self):
        """Não é "sob demanda": a câmera está com o pipeline, e a Intelbras aceita
        uma conexão RTSP só — uma captura direta agora falharia."""
        pipeline._instancias[CAM] = _PipelineFalso(iniciando=True)
        assert pipeline.estado_stream(CAM) == "aquecendo"

    def test_frame_velho_com_thread_viva_e_aquecendo(self):
        pipeline._instancias[CAM] = _PipelineFalso()
        _publicar_frame()
        estado.ultimo_frame_ts[CAM] = time.time() - (pipeline.FRAME_VIVO_MAX_IDADE_SEG + 1)
        assert pipeline.estado_stream(CAM) == "aquecendo"

    def test_thread_morta_e_sem_frame_volta_para_sob_demanda(self):
        """Pipeline caiu: até o supervisor reerguer, a captura direta é a única
        chance de imagem — e se falhar, a tela mostra o erro dela."""
        pipeline._instancias[CAM] = _PipelineFalso(thread_viva=False)
        assert pipeline.estado_stream(CAM) == "sob_demanda"


class TestAguardarFrame:
    def test_devolve_false_quando_nao_ha_frame(self):
        t0 = time.time()
        assert stream.aguardar_frame_camera(CAM, timeout=0.3) is False
        assert time.time() - t0 >= 0.3      # esperou, não devolveu na hora

    def test_devolve_true_assim_que_ha_frame(self):
        _publicar_frame()
        assert stream.aguardar_frame_camera(CAM, timeout=5) is True


class TestRotaDoStream:
    """Ponta a ponta: é a resposta HTTP que precisa denunciar a falta de imagem."""

    def test_sem_frame_responde_503_com_motivo(self, admin, posto, monkeypatch):
        monkeypatch.setattr(stream, "ESPERA_PRIMEIRO_FRAME_SEG", 0.2)
        r = admin.get(f"/stream/{posto['camera_id']}.mjpg")
        assert r.status_code == 503
        assert "quadro" in r.json()["detail"]

    def test_camera_inexistente_responde_404(self, admin):
        assert admin.get("/stream/99999.mjpg").status_code == 404

    def test_detalhe_da_camera_nao_promete_ao_vivo_sem_pipeline(self, admin, posto):
        """Era daqui que vinha a mentira que apontava o <img> para um MJPEG mudo."""
        d = admin.get(f"/api/cameras/{posto['camera_id']}/detalhe").json()
        assert d["stream_modo"] == "sob_demanda"
        assert d["ao_vivo"] is False

    def test_posto_nao_promete_ao_vivo_sem_pipeline(self, admin, posto):
        d = admin.get(f"/api/postos/{posto['empresa_id']}").json()
        assert [c["stream_modo"] for c in d["cameras"]] == ["sob_demanda"]
        assert all(c["ao_vivo"] is False for c in d["cameras"])


class TestGeradorNaoGiraParaSempre:
    def test_encerra_apos_tempo_sem_frame(self, monkeypatch):
        """Antes o gerador girava eternamente sem emitir nada quando a câmera parava:
        a conexão ficava aberta segurando uma thread do pool e o navegador continuava
        exibindo o último quadro como se fosse atual."""
        monkeypatch.setattr(stream, "PARADA_SEM_FRAME_SEG", 0.2)
        _publicar_frame()
        g = stream.gerar_mjpeg_camera(CAM, fps_max=50)
        assert next(g).startswith(b"--frame")
        estado.esquecer_camera(CAM)          # câmera parou de publicar
        with pytest.raises(StopIteration):
            next(g)


class TestWatchdogOlhaAcameraNaoAEmissao:
    """O corte de `gerar_mjpeg_camera` mede `estado.ultimo_frame_ts`, não "quando emiti".

    Medir a emissão não detectava nada: `obter_frame_camera` devolve o último frame para
    sempre e `_jpeg_cacheado` devolve o JPEG guardado quando o objeto é o mesmo — então
    havia sempre o que emitir e o prazo era renovado a cada volta. Câmera morta com frame
    velho em memória era servida a 15 fps indefinidamente, prendendo uma thread do pool por
    viewer e mostrando imagem congelada como se fosse ao vivo.
    """

    def test_frame_velho_encerra_o_stream(self, monkeypatch):
        monkeypatch.setattr(stream, "PARADA_SEM_FRAME_SEG", 0.2)
        estado.frames_cameras[CAM] = np.zeros((16, 16, 3), dtype=np.uint8)
        # A câmera publicou, mas há muito tempo — é o estado de câmera morta.
        estado.ultimo_frame_ts[CAM] = time.time() - 60

        emitidos = list(stream.gerar_mjpeg_camera(CAM, fps_max=50))

        assert emitidos, "deve emitir o que tem antes de desistir"
        assert len(emitidos) < 20, "não pode servir o quadro congelado para sempre"

    def test_frame_fresco_mantem_o_stream(self, monkeypatch):
        """Não pode cortar stream saudável: o gerador é infinito enquanto houver frame
        novo, então consumimos só as primeiras iterações."""
        import itertools
        monkeypatch.setattr(stream, "PARADA_SEM_FRAME_SEG", 0.5)
        estado.frames_cameras[CAM] = np.zeros((16, 16, 3), dtype=np.uint8)
        estado.ultimo_frame_ts[CAM] = time.time()

        primeiros = list(itertools.islice(stream.gerar_mjpeg_camera(CAM, fps_max=50), 5))
        assert len(primeiros) == 5

    def test_camera_que_nunca_publicou_ainda_encerra(self, monkeypatch):
        """Sem timestamp não há referência; sem o fallback para o início do stream o laço
        giraria para sempre — o oposto do que o corte existe para impedir."""
        monkeypatch.setattr(stream, "PARADA_SEM_FRAME_SEG", 0.2)
        estado.frames_cameras.pop(CAM, None)
        estado.ultimo_frame_ts.pop(CAM, None)

        assert list(stream.gerar_mjpeg_camera(CAM, fps_max=50)) == []
