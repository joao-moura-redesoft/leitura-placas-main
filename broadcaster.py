"""Hub WebSocket — recebe eventos do pipeline (thread síncrona) e empurra
para todos os clientes conectados (loop asyncio).

Uso:
    # No startup do servidor:
    broadcaster.registrar_loop(asyncio.get_running_loop())

    # No endpoint WebSocket:
    await broadcaster.conectar(websocket)
    try:
        while True: await websocket.receive_text()
    finally:
        broadcaster.desconectar(websocket)

    # No pipeline (thread síncrona):
    broadcaster.push({"tipo": "deteccao", "placa": ..., ...})
"""
from __future__ import annotations

import asyncio
import logging
from fastapi import WebSocket

log = logging.getLogger(__name__)


class Broadcaster:
    def __init__(self) -> None:
        self._clientes: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def registrar_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def conectar(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clientes.add(ws)
        log.debug("WS conectado — %d cliente(s)", len(self._clientes))

    def desconectar(self, ws: WebSocket) -> None:
        self._clientes.discard(ws)
        log.debug("WS desconectado — %d cliente(s)", len(self._clientes))

    async def _enviar_todos(self, data: dict) -> None:
        mortos: set[WebSocket] = set()
        for ws in list(self._clientes):
            try:
                await ws.send_json(data)
            except Exception:
                mortos.add(ws)
        self._clientes -= mortos

    def push(self, data: dict) -> None:
        """Chamado de qualquer thread. Agenda envio no loop asyncio."""
        if not self._clientes:
            return
        if not self._loop or not self._loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(self._enviar_todos(data), self._loop)


broadcaster = Broadcaster()
