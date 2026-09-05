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
import threading
from fastapi import WebSocket

log = logging.getLogger(__name__)


class Broadcaster:
    def __init__(self) -> None:
        self._clientes: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        # Protege leituras/escritas de `_clientes`: `push()` roda numa thread síncrona
        # (chamada pelo pipeline de visão) enquanto `conectar`/`desconectar`/
        # `_enviar_todos` mutam o mesmo set dentro do loop asyncio. O GIL evita
        # corrupção da estrutura, mas não evita a corrida lógica de `push()` checar
        # "sem clientes" bem no instante em que um cliente novo está conectando e
        # perder aquele evento. Seções críticas são só leitura/cópia/add/remove do
        # set — nunca I/O de rede — para não segurar o lock durante envio.
        self._lock = threading.Lock()

    def registrar_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def conectar(self, ws: WebSocket) -> None:
        await ws.accept()
        with self._lock:
            self._clientes.add(ws)
            total = len(self._clientes)
        log.debug("WS conectado: %d cliente(s)", total)

    def desconectar(self, ws: WebSocket) -> None:
        with self._lock:
            self._clientes.discard(ws)
            total = len(self._clientes)
        log.debug("WS desconectado: %d cliente(s)", total)

    async def _enviar_todos(self, data: dict) -> None:
        with self._lock:
            destinatarios = list(self._clientes)
        mortos: set[WebSocket] = set()
        for ws in destinatarios:
            try:
                await ws.send_json(data)
            except Exception:
                mortos.add(ws)
        if mortos:
            with self._lock:
                self._clientes -= mortos

    def push(self, data: dict) -> None:
        """Chamado de qualquer thread. Agenda envio no loop asyncio."""
        with self._lock:
            tem_clientes = bool(self._clientes)
        if not tem_clientes:
            return
        if not self._loop or not self._loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(self._enviar_todos(data), self._loop)


broadcaster = Broadcaster()
