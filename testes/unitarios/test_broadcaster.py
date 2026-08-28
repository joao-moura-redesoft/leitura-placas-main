"""Regressão: `push()` roda numa thread síncrona (chamada pelo pipeline de visão) e
lia `self._clientes`/`self._loop` sem lock, enquanto `conectar`/`desconectar`/
`_enviar_todos` mutam o mesmo set dentro do loop asyncio. O comportamento observável
que importa preservar: toda mensagem enviada por `push()` chega a todo cliente
conectado no momento do push, e conectar/desconectar/push concorrentes de várias
threads nunca lançam exceção.
"""
from __future__ import annotations
import asyncio
import threading
import time

import pytest

from app.core.broadcaster import Broadcaster


@pytest.fixture
def loop_asyncio():
    """Loop asyncio rodando de verdade numa thread própria — `push()` depende de um
    loop vivo (`run_coroutine_threadsafe`), não dá pra testar com um loop parado."""
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2)
    loop.close()


class _WSFalso:
    """Dublê mínimo de fastapi.WebSocket: só o que o Broadcaster chama."""

    def __init__(self) -> None:
        self.recebidos: list[dict] = []

    async def accept(self) -> None:
        pass

    async def send_json(self, data: dict) -> None:
        self.recebidos.append(data)


def _rodar(loop: asyncio.AbstractEventLoop, coro):
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=2)


def _esperar(pred, timeout=2.0):
    fim = time.time() + timeout
    while time.time() < fim:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class TestEntregaBasica:
    def test_push_entrega_a_cliente_conectado(self, loop_asyncio):
        bc = Broadcaster()
        bc.registrar_loop(loop_asyncio)
        ws = _WSFalso()
        _rodar(loop_asyncio, bc.conectar(ws))

        bc.push({"tipo": "deteccao"})

        assert _esperar(lambda: ws.recebidos == [{"tipo": "deteccao"}])

    def test_push_sem_clientes_nao_levanta(self, loop_asyncio):
        bc = Broadcaster()
        bc.registrar_loop(loop_asyncio)
        bc.push({"tipo": "x"})  # não deve levantar mesmo sem ninguém conectado

    def test_push_sem_loop_registrado_nao_levanta(self):
        bc = Broadcaster()
        bc.push({"tipo": "x"})

    def test_desconectar_remove_do_set_e_para_de_receber(self, loop_asyncio):
        bc = Broadcaster()
        bc.registrar_loop(loop_asyncio)
        ws = _WSFalso()
        _rodar(loop_asyncio, bc.conectar(ws))
        bc.desconectar(ws)

        bc.push({"tipo": "x"})
        time.sleep(0.1)

        assert ws.recebidos == []

    def test_push_entrega_a_varios_clientes(self, loop_asyncio):
        bc = Broadcaster()
        bc.registrar_loop(loop_asyncio)
        clientes = [_WSFalso() for _ in range(5)]
        for ws in clientes:
            _rodar(loop_asyncio, bc.conectar(ws))

        bc.push({"tipo": "y"})

        assert _esperar(lambda: all(c.recebidos == [{"tipo": "y"}] for c in clientes))


class TestConcorrencia:
    def test_conectar_desconectar_e_push_concorrentes_nao_quebram(self, loop_asyncio):
        """`_clientes` é mutado de threads diferentes (pipeline síncrono via `push`,
        loop asyncio via `conectar`/`desconectar`) — o lock precisa manter isso
        seguro sob concorrência real, sem exceção nem travamento."""
        bc = Broadcaster()
        bc.registrar_loop(loop_asyncio)
        parar = threading.Event()
        erros: list[Exception] = []

        def _conectar_e_desconectar():
            while not parar.is_set():
                ws = _WSFalso()
                try:
                    _rodar(loop_asyncio, bc.conectar(ws))
                    bc.desconectar(ws)
                except Exception as e:  # pragma: no cover - só se quebrar
                    erros.append(e)

        def _empurrar():
            while not parar.is_set():
                try:
                    bc.push({"tipo": "z"})
                except Exception as e:  # pragma: no cover - só se quebrar
                    erros.append(e)

        threads = (
            [threading.Thread(target=_conectar_e_desconectar) for _ in range(3)]
            + [threading.Thread(target=_empurrar) for _ in range(3)]
        )
        for t in threads:
            t.start()
        time.sleep(0.3)
        parar.set()
        for t in threads:
            t.join(timeout=2)

        assert erros == []
