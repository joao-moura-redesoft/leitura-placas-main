"""O loop de eventos do servidor HTTP, e por que ele NÃO pode ser o Proactor no Windows.

Em 04/09/2026 o painel ficou inacessível com o processo vivo: nenhum socket em escuta na
porta 14000, o loop de eventos girando, as câmeras lendo placa, o `lifespan` nunca
desligado. Aquela queda específica veio pelo desligamento que não termina — está no último
teste deste arquivo, e o log prova (não há "Accept failed on a socket" nele). Mas o MESMO
estado, indistinguível de fora, tem um segundo caminho, e é o motivo do resto do arquivo:
`asyncio/proactor_events.py::_start_serving` trata QUALQUER `OSError` no accept assim:

    except OSError as exc:
        if sock.fileno() != -1:
            self.call_exception_handler({'message': 'Accept failed on a socket', ...})
            sock.close()          # <- fecha o socket de ESCUTA
        ...
        # e o `loop()` nunca é reagendado: fim dos accepts, para sempre

Um cliente que dá RST no meio do handshake (navegador abandonando `/stream/N.mjpg`, que a
vitrine da feira faz a cada carregamento) produz exatamente esse `OSError`. A Selector, no
mesmo lugar, ignora ou reagenda — o socket de escuta nunca morre por causa de um cliente.

`app/servidor.py` já pedia a Selector via `set_event_loop_policy`, e isso NÃO bastava: o
uvicorn cria o loop com `loop_factory=` desde a 0.36, e `loop_factory` passa por cima da
política. É essa armadilha — política que parece resolver e não resolve — que este arquivo
tranca: os testes perguntam pelo loop à FÁBRICA DO UVICORN, não à política, porque foi
justamente olhar a política que deu a falsa sensação de estar resolvido.
"""
from __future__ import annotations

import asyncio
import errno
import platform
import socket
import threading
import time

import pytest
import uvicorn

from app.servidor import FABRICA_LOOP, TIMEOUT_SHUTDOWN_SEG

NO_WINDOWS = platform.system() == "Windows"
so_windows = pytest.mark.skipif(not NO_WINDOWS, reason="a armadilha da Proactor é do Windows")


def _loop_de(loop_config: str) -> asyncio.AbstractEventLoop:
    """O loop que o uvicorn REALMENTE criaria para `loop=<loop_config>`.

    Passa pelo `Config.get_loop_factory()` do uvicorn de propósito: é ali que a política de
    event loop do processo é ignorada, e testar a nossa string sem passar por ele mediria
    uma coisa que o servidor não faz.
    """
    async def app(scope, receive, send):    # pragma: no cover - nunca chamado
        raise AssertionError

    fabrica = uvicorn.Config(app=app, loop=loop_config).get_loop_factory()
    assert fabrica is not None, f"uvicorn não devolveu fábrica para loop={loop_config!r}"
    return fabrica()


class TestQualLoopOServidorUsa:
    @so_windows
    def test_fabrica_do_projeto_nao_e_proactor(self):
        loop = _loop_de(FABRICA_LOOP)
        try:
            assert not isinstance(loop, asyncio.ProactorEventLoop), (
                "o servidor voltou a subir na Proactor: um RST no accept fecha o socket "
                "de escuta e o painel some com o processo vivo"
            )
            assert isinstance(loop, asyncio.SelectorEventLoop)
        finally:
            loop.close()

    @so_windows
    def test_o_default_do_uvicorn_seria_proactor(self):
        """Documenta o que estamos corrigindo: com `loop="auto"` (o default) o uvicorn
        entrega a Proactor no Windows. Se algum dia a upstream mudar isso, este teste
        falha e a nossa imposição pode ser reavaliada — em vez de ficar para sempre."""
        loop = _loop_de("auto")
        try:
            assert isinstance(loop, asyncio.ProactorEventLoop)
        finally:
            loop.close()

    @so_windows
    def test_politica_do_processo_nao_decide_nada_sozinha(self):
        """O núcleo da armadilha: a política pede Selector e a fábrica do uvicorn devolve
        Proactor de qualquer jeito. Enquanto isso for verdade, `FABRICA_LOOP` é obrigatório;
        um teste que só olhasse a política teria passado com o bug em produção."""
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        loop = _loop_de("auto")
        try:
            assert isinstance(loop, asyncio.ProactorEventLoop)
        finally:
            loop.close()

    def test_shutdown_tem_teto(self):
        """`timeout_graceful_shutdown=None` (o default do uvicorn) é esperar PARA SEMPRE, e
        o `/stream/N.mjpg` deste servidor só termina depois de 20 s sem frame novo — nunca,
        com a câmera viva. Sem teto, o desligamento fechava a porta e travava antes do
        `lifespan.shutdown()`: mesmo sintoma, outro caminho."""
        assert TIMEOUT_SHUTDOWN_SEG is not None
        assert 0 < TIMEOUT_SHUTDOWN_SEG <= 60


# ── O comportamento de que tudo isto depende ────────────────────────────────
#
# Por que aqui não há teste "sobe servidor, dá 200 RSTs, vê se a porta vive": foi medido, e
# não discrimina. A falha da Proactor depende da janela em que o RST cai em relação ao
# AcceptEx: em três execuções iguais, uma sobreviveu a 2000 RSTs em 2 s e outra morreu na
# primeira rodada de 200 — e, quando morre, cada `connect` seguinte fica ~70 s em SYN_SENT,
# então o teste ora passa com o bug presente, ora demora um minuto e meio. Um teste que
# pode passar com o defeito no lugar é pior do que nenhum (ver
# `testes/reproduzir_accept_proactor.py`, que é a reprodução manual, com essa ressalva).
#
# O que dá para trancar sem loteria é a REGRA: o loop que o servidor usa não fecha o socket
# de escuta quando um accept falha. É o que este teste faz, sem rede nenhuma.

class _SocketDeMentira:
    """Socket de escuta que só sabe falhar no `accept` e anotar se foi fechado."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.fechado = False

    def accept(self):
        raise self.exc

    def close(self) -> None:
        self.fechado = True

    def fileno(self) -> int:
        return 4242

    def getsockname(self):          # pragma: no cover - só o repr do asyncio usa
        return ("0.0.0.0", 14000)


def test_desligar_nao_fica_pendurado_num_stream_que_nunca_termina():
    """O caminho da queda de 04/09/2026, encurralado num teste de 3 segundos.

    `Server.shutdown()` fecha o socket de escuta ANTES de esperar as requisições em voo, e
    com `timeout_graceful_shutdown=None` (o default) essa espera é infinita. Um
    `/stream/N.mjpg` nunca termina sozinho enquanto a câmera publica, então o servidor
    ficava exatamente como foi encontrado: porta fechada, `lifespan.shutdown()` nunca
    chamado, câmeras rodando, processo vivo, nada no log.

    Aqui o stream infinito é um ASGI de duas linhas e o teto é 2 s (o de produção é
    `TIMEOUT_SHUTDOWN_SEG`, verificado acima). Sem teto, o `join` abaixo nunca voltaria.
    """
    async def app_infinito(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while True:
            await send({"type": "http.response.body", "body": b"x" * 1024,
                        "more_body": True})
            await asyncio.sleep(0.05)

    with socket.socket() as tmp:
        tmp.bind(("127.0.0.1", 0))
        porta = tmp.getsockname()[1]

    servidor = uvicorn.Server(uvicorn.Config(
        app=app_infinito, host="127.0.0.1", port=porta, loop=FABRICA_LOOP,
        log_level="warning", timeout_graceful_shutdown=2,
    ))
    t = threading.Thread(target=servidor.run, name="uvicorn-stream-infinito", daemon=True)
    t.start()
    limite = time.time() + 20
    while not servidor.started and time.time() < limite:
        time.sleep(0.05)
    assert servidor.started, "o servidor de teste não subiu"

    cliente = socket.create_connection(("127.0.0.1", porta), timeout=5)
    try:
        cliente.sendall(b"GET /stream HTTP/1.1\r\nHost: x\r\n\r\n")
        cliente.settimeout(5)
        assert cliente.recv(64), "o stream de teste não começou"

        servidor.should_exit = True         # o que um sinal faria, sem matar o pytest
        t.join(timeout=20)
        assert not t.is_alive(), (
            "o desligamento ficou pendurado no stream em voo — em produção isso é a porta "
            "fechada com o processo vivo, para sempre"
        )
    finally:
        servidor.should_exit = True
        cliente.close()
        t.join(timeout=20)


def test_selector_nao_fecha_o_socket_de_escuta_quando_o_accept_falha():
    """A regra que faz o painel continuar de pé — e o motivo de `FABRICA_LOOP` existir.

    `_accept_connection` da Selector devolve o erro para o loop tratar (que loga e segue) e
    NÃO toca no socket de escuta. A Proactor, no mesmo lugar, chama `sock.close()` e nunca
    reagenda o accept: é a diferença entre "um cliente deu RST" e "o servidor não existe
    mais". Se um dia a Selector passar a fechar o socket, este teste falha aqui, e não
    depois de uma hora de painel fora do ar.
    """
    loop = asyncio.SelectorEventLoop()
    sock = _SocketDeMentira(OSError(errno.EINVAL, "accept falhou"))
    try:
        with pytest.raises(OSError):
            loop._accept_connection(lambda: None, sock)
        assert not sock.fechado, (
            "a Selector fechou o socket de escuta por causa de um accept com erro — "
            "com isso o servidor para de aceitar conexão para sempre"
        )
    finally:
        loop.close()
