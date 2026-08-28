"""app/core/estado.py: agregados por câmera não podem sobreviver à câmera que os gerou.

Achado B5 do review de 28/08/2026: `pipeline_rodando`/`camera_conectada`/`fps_atual` eram
globals mantidas manualmente em sincronia com os dicts por câmera — e `esquecer_camera`
fazia `pop()` direto nos dicts sem recomputar o agregado, deixando "pipeline rodando"
fantasma até a próxima chamada de `marcar_pipeline`. Viraram funções computadas na hora
da leitura para eliminar a classe inteira do problema.
"""
from __future__ import annotations

from app.core import estado


def test_esquecer_camera_nao_deixa_pipeline_rodando_fantasma():
    estado.marcar_pipeline(7, True)
    assert estado.pipeline_rodando() is True

    estado.esquecer_camera(7)

    assert estado.pipeline_rodando() is False, \
        "esquecer_camera não pode deixar o agregado desatualizado"


def test_esquecer_camera_nao_deixa_camera_conectada_fantasma():
    estado.marcar_conexao(7, True)
    assert estado.camera_conectada() is True

    estado.esquecer_camera(7)

    assert estado.camera_conectada() is False


def test_pipeline_rodando_reflete_qualquer_camera_viva():
    estado.marcar_pipeline(1, True)
    estado.marcar_pipeline(2, False)
    try:
        assert estado.pipeline_rodando() is True
        estado.marcar_pipeline(1, False)
        assert estado.pipeline_rodando() is False
    finally:
        estado.esquecer_camera(1)
        estado.esquecer_camera(2)


def test_fps_atual_soma_as_cameras_vivas():
    estado.atualizar_fps(10.0, camera_id=1)
    estado.atualizar_fps(5.5, camera_id=2)
    try:
        assert estado.fps_atual() == 15.5
    finally:
        estado.esquecer_camera(1)
        estado.esquecer_camera(2)
