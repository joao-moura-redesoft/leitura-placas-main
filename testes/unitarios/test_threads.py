"""app/core/threads.encerrar_thread — helper compartilhado por WorkerSupervisor.parar,
RetentionWorker.parar, _Encoder.parar e parar_coletor (achado B2 do review de 28/08/2026).
"""
from __future__ import annotations

import threading
import time

from app.core.threads import encerrar_thread


def test_thread_none_e_tratada_como_ja_encerrada():
    chamou = []
    assert encerrar_thread(None, 1.0, lambda: chamou.append(1)) is True
    assert chamou == []


def test_thread_ja_morta_e_tratada_como_encerrada():
    t = threading.Thread(target=lambda: None)
    t.start()
    t.join()
    chamou = []
    assert encerrar_thread(t, 1.0, lambda: chamou.append(1)) is True
    assert chamou == []


def test_thread_que_termina_dentro_do_prazo_nao_chama_ao_expirar():
    liberar = threading.Event()
    t = threading.Thread(target=liberar.wait)
    t.start()
    liberar.set()
    chamou = []
    assert encerrar_thread(t, 2.0, lambda: chamou.append(1)) is True
    assert chamou == []


def test_thread_presa_estoura_o_timeout_e_chama_ao_expirar():
    preso = threading.Event()
    t = threading.Thread(target=preso.wait, daemon=True)
    t.start()
    try:
        chamou = []
        assert encerrar_thread(t, 0.05, lambda: chamou.append(1)) is False
        assert chamou == [1]
    finally:
        preso.set()
        t.join(timeout=1.0)
