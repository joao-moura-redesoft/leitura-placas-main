"""`POST /api/config` não pode travar a resposta HTTP esperando o coletor de dataset.

Achado A6 do review de 28/08/2026: `config_salvar` chamava `iniciar_coletor` direto no
corpo da request; `iniciar_coletor` chama `parar_coletor()` primeiro, que faz `join`
SEQUENCIAL de até 5s por câmera — pior caso N_câmeras × 5s bloqueando a rota.
"""
from __future__ import annotations

import threading
import time


def test_config_salvar_nao_espera_o_coletor_de_dataset(admin, ambiente, monkeypatch):
    liberar = threading.Event()

    def _coletor_lento(cfg):
        liberar.wait(timeout=2)

    from app.visao import captura_dataset as cap_mod
    monkeypatch.setattr(cap_mod, "iniciar_coletor", _coletor_lento)

    inicio = time.time()
    r = admin.post("/api/config", json={"log_level": "info"})
    duracao = time.time() - inicio

    assert r.status_code == 200, r.text
    assert duracao < 1.0, "config_salvar não pode bloquear esperando o coletor de dataset"

    liberar.set()   # não deixa a thread de fundo presa além do teste
