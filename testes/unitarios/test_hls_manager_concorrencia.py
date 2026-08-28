"""HLSManager: `_encoders`/`_ativo` protegidos por lock — achado do review de 28/08/2026.

O supervisor chama `revisar()` a cada 5s numa thread de fundo; uma request HTTP
(`config_salvar` trocando `streaming_modo`, ou o CRUD de câmeras) pode chamar
`parar()`/`iniciar()`/`adicionar_camera()`/`remover_camera()` ao mesmo tempo. Sem lock,
um encoder podia ficar órfão (processo ffmpeg vivo, mas sem referência em `_encoders`
pra pará-lo depois).

Os testes usam um `_Encoder` FALSO — sem thread real nem ffmpeg — para poder rodar
centenas de ciclos rápido e martelar a concorrência de verdade.
"""
from __future__ import annotations

import threading
import time

import pytest

from app.streaming import hls_encoder


class _EncoderFalso:
    """Substitui `_Encoder`: sem thread nem subprocess, só contabiliza chamadas."""

    def __init__(self, camera_id: int) -> None:
        self.camera_id = camera_id
        self.parado = False
        self._morta = False
        self._stop = threading.Event()   # HLSManager.parar() acessa isto direto

    def iniciar(self) -> None:
        pass

    def parar(self, timeout: float = 8.0) -> bool:
        self.parado = True
        return True

    def morto(self) -> bool:
        return self._morta


@pytest.fixture
def manager(monkeypatch, tmp_path):
    monkeypatch.setattr(hls_encoder, "_Encoder", _EncoderFalso)
    monkeypatch.setattr(hls_encoder, "ffmpeg_disponivel", lambda: True)
    monkeypatch.setattr(hls_encoder, "HLS_DIR", tmp_path / "hls")
    return hls_encoder.HLSManager()


CAMERAS = [{"id": 1, "ativo": True}, {"id": 2, "ativo": True}]


def test_iniciar_ativo_parar_contrato_basico(manager):
    assert manager.iniciar(CAMERAS) is True
    assert manager.ativo() is True
    assert set(manager._encoders) == {1, 2}

    manager.parar()

    assert manager.ativo() is False
    assert manager._encoders == {}


def test_remover_camera_inexistente_e_nao_op(manager):
    manager.iniciar(CAMERAS)
    manager.remover_camera(999)   # não pode levantar nem afetar as outras
    assert set(manager._encoders) == {1, 2}


def test_revisar_remove_camera_desativada(manager):
    manager.iniciar(CAMERAS)
    manager.revisar([{"id": 1, "ativo": True}, {"id": 2, "ativo": False}])
    assert set(manager._encoders) == {1}


def test_ffmpeg_ausente_nao_marca_ativo(manager, monkeypatch):
    monkeypatch.setattr(hls_encoder, "ffmpeg_disponivel", lambda: False)
    assert manager.iniciar(CAMERAS) is False
    assert manager.ativo() is False


class TestConcorrencia:
    def test_revisar_e_parar_iniciar_simultaneos_nao_deixam_encoder_orfao(self, manager):
        """Martela `revisar()` (como o supervisor faria a cada 5s) ao mesmo tempo que
        `parar()`+`iniciar()` (como um config_salvar trocando streaming_modo faria) — sem
        lock, um encoder podia sobreviver fora de `_encoders` depois do `parar()`."""
        manager.iniciar(CAMERAS)
        parar_tudo = threading.Event()
        erros = []

        def martelar_revisar():
            while not parar_tudo.is_set():
                try:
                    manager.revisar(CAMERAS)
                except Exception as e:   # pragma: no cover - só se a corrida voltar
                    erros.append(e)

        def martelar_parar_iniciar():
            for _ in range(50):
                try:
                    manager.parar()
                    manager.iniciar(CAMERAS)
                except Exception as e:   # pragma: no cover
                    erros.append(e)

        t_revisar = threading.Thread(target=martelar_revisar)
        t_revisar.start()
        martelar_parar_iniciar()
        parar_tudo.set()
        t_revisar.join(timeout=5)

        assert not erros, f"exceção durante a concorrência: {erros}"
        # Invariante final: todo encoder que sobrou em `_encoders` é o mesmo objeto que
        # `iniciar()`/`revisar()` acabaram de colocar lá — nada "por fora" sobrevivendo.
        assert set(manager._encoders) <= {1, 2}
