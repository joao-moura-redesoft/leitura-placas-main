"""Reproduz, na mão, a falha que fazia a porta 14000 desaparecer com o processo vivo.

Por que existe: em 04/09/2026 o painel ficou inacessível às 10:35 e o processo seguiu de pé
por mais de uma hora — loop de eventos girando, câmeras lendo placa, banco gravando, e
`netstat` sem UM socket em escuta na porta. A causa é o accept da Proactor (Windows):

    # asyncio/proactor_events.py, _start_serving
    except OSError as exc:
        if sock.fileno() != -1:
            self.call_exception_handler({'message': 'Accept failed on a socket', ...})
            sock.close()      # fecha o socket de ESCUTA
        # e o `loop()` interno nunca é reagendado: fim dos accepts, para sempre

Um cliente que dá RST no meio do handshake basta. É o que um navegador faz ao abandonar um
`/stream/N.mjpg` — a vitrine da feira abre um por carregamento e larga o anterior.

Por que NÃO é teste automatizado: a falha depende da janela em que o RST cai em relação ao
AcceptEx. Medido em três execuções idênticas: uma sobreviveu a 2000 RSTs em 2 s, outra
morreu na primeira rodada de 200. Pior, depois de morta cada `connect` novo fica ~70 s em
SYN_SENT no loopback, então o teste ora passa com o defeito presente, ora leva minutos. A
regra determinística está trancada em `testes/unitarios/test_loop_do_servidor.py`; este
script é para reproduzir o fenômeno com os próprios olhos.

Uso:

    python testes/reproduzir_accept_proactor.py auto                  # deve MORRER (Proactor)
    python testes/reproduzir_accept_proactor.py asyncio:SelectorEventLoop   # deve SOBREVIVER

Repita algumas vezes: com `auto` a morte é frequente, não garantida. Ao morrer, o script
imprime em qual rodada foi — e é exatamente o estado em que a aplicação foi encontrada.
"""
from __future__ import annotations

import os
import socket
import struct
import sys
import threading
import time

import uvicorn

RODADAS = 10
RSTS_POR_RODADA = 200


async def _app(scope, receive, send):
    if scope["type"] != "http":
        return
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _atende(porta: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", porta), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(b"GET / HTTP/1.0\r\nHost: x\r\nConnection: close\r\n\r\n")
            return b" 200 " in s.recv(64)
    except OSError:
        return False


def _rst(porta: int) -> None:
    """Conecta e fecha com SO_LINGER=0: RST, sem FIN."""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    try:
        s.connect(("127.0.0.1", porta))
    except OSError:
        pass
    finally:
        s.close()


def main() -> None:
    loop_cfg = sys.argv[1] if len(sys.argv) > 1 else "auto"
    porta = _porta_livre()
    servidor = uvicorn.Server(uvicorn.Config(
        app=_app, host="127.0.0.1", port=porta, loop=loop_cfg,
        log_level="warning", timeout_graceful_shutdown=5,
    ))
    threading.Thread(target=servidor.run, daemon=True).start()

    limite = time.time() + 20
    while not servidor.started and time.time() < limite:
        time.sleep(0.05)
    if not servidor.started or not _atende(porta):
        print(f"loop={loop_cfg}: o servidor de teste nem subiu — nada a medir")
        os._exit(2)

    print(f"loop={loop_cfg}, porta {porta}: atendendo. Batendo com RSTs...", flush=True)
    inicio = time.time()
    for rodada in range(1, RODADAS + 1):
        for _ in range(RSTS_POR_RODADA):
            _rst(porta)
        if not _atende(porta):
            print(f"loop={loop_cfg}: MORREU na rodada {rodada} "
                  f"({rodada * RSTS_POR_RODADA} RSTs, {time.time() - inicio:.1f}s) — "
                  f"socket de escuta fechado pelo accept, processo vivo", flush=True)
            os._exit(1)
    print(f"loop={loop_cfg}: SOBREVIVEU {RODADAS * RSTS_POR_RODADA} RSTs "
          f"({time.time() - inicio:.1f}s)", flush=True)
    # `os._exit`: o servidor está num thread daemon com um loop próprio; sair pela porta da
    # frente aqui só serviria para esperar por ele.
    os._exit(0)


if __name__ == "__main__":
    main()
