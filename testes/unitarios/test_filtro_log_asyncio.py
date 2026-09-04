"""`_FiltroQuedaDePeer` — o filtro que apaga o traceback de conexão fechada pelo peer.

Um filtro que SILENCIA log é a coisa mais fácil de quebrar sem ninguém notar: o modo de
falha não é uma exceção, é um erro de verdade que deixa de aparecer no `alpr.log`. Por isso
o que este arquivo trava não é "o reset é descartado" (o efeito desejado, fácil) e sim o
contrário — que exceção que NÃO é queda de peer continua chegando ao handler.

Os records passam pela máquina de logging de verdade, não por uma chamada direta a
`.filter()`: filtro de logger roda dentro de `Logger.handle`, e é justamente esse
acoplamento (filtro instalado no logger certo, antes dos handlers) que se quer verificar.
"""
from __future__ import annotations

import logging

import pytest

from app.servidor import _FiltroQuedaDePeer, _silenciar_queda_de_peer


@pytest.fixture
def asyncio_log():
    """O logger `asyncio` com o filtro instalado e um handler que guarda os records.

    Desfaz tudo no teardown: `logging` é estado global do processo, e deixar filtro ou
    handler pendurado vazaria para os outros testes da sessão.
    """
    lg = logging.getLogger("asyncio")
    capturados: list[logging.LogRecord] = []

    class _Coletor(logging.Handler):
        def emit(self, record):
            capturados.append(record)

    handler = _Coletor()
    filtro = _FiltroQuedaDePeer()
    nivel_antes, propaga_antes = lg.level, lg.propagate
    lg.addFilter(filtro)
    lg.addHandler(handler)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False          # não suja a saída do pytest com os records de teste
    try:
        yield lg, capturados
    finally:
        lg.removeFilter(filtro)
        lg.removeHandler(handler)
        lg.setLevel(nivel_antes)
        lg.propagate = propaga_antes


def _logar_excecao(lg, exc: BaseException, mensagem: str | None = None) -> None:
    """Reproduz o que o `call_exception_handler` do asyncio faz: erro com exc_info.

    `mensagem` existe porque o filtro passou a olhar TAMBÉM o texto: a mesma exceção é
    ruído vindo de um transporte de conexão e é fatal vindo do socket de escuta.
    """
    try:
        raise exc
    except BaseException:
        lg.error(mensagem or ("Exception in callback _ProactorBasePipeTransport."
                              "_call_connection_lost(None)"), exc_info=True)


class TestOQueDeveContinuarAparecendo:
    """O lado que importa: o filtro não pode engolir erro de verdade."""

    def test_excecao_que_nao_e_queda_de_peer_passa(self, asyncio_log):
        lg, capturados = asyncio_log
        _logar_excecao(lg, ValueError("erro de verdade no callback"))
        assert len(capturados) == 1
        assert isinstance(capturados[0].exc_info[1], ValueError)

    def test_erro_sem_exc_info_passa(self, asyncio_log):
        """O filtro olha `exc_info`; record sem exceção nenhuma não é assunto dele."""
        lg, capturados = asyncio_log
        lg.error("loop travado por 3s — nada a ver com conexão")
        assert len(capturados) == 1

    def test_accept_failed_passa_mesmo_sendo_reset(self, asyncio_log):
        """A exceção é a MESMA (`ConnectionResetError`), mas vindo do socket que ESCUTA ela
        é a falha mais grave que este servidor tem: `_start_serving` da Proactor fecha o
        socket de escuta e nunca mais reagenda o accept — porta 14000 deixa de existir com
        o processo vivo. Era o único registro do evento, e o filtro o descartava."""
        lg, capturados = asyncio_log
        _logar_excecao(lg, ConnectionResetError(10054, "reset no meio do accept"),
                       mensagem="Accept failed on a socket\nsocket: <asyncio.TransportSocket "
                                "fd=360, laddr=('0.0.0.0', 14000)>")
        assert len(capturados) == 1
        assert "Accept failed" in capturados[0].getMessage()

    def test_oserror_generico_passa(self, asyncio_log):
        """`OSError` é a MÃE das três silenciadas. Silenciar a base levaria embora
        `PermissionError`, `FileNotFoundError` e todo o resto de I/O — o teste existe para
        que trocar a tupla pela superclasse quebre aqui, e não em produção."""
        lg, capturados = asyncio_log
        _logar_excecao(lg, OSError("disco cheio"))
        assert len(capturados) == 1


class TestOQueDeveSerDescartado:
    @pytest.mark.parametrize("exc", [
        # O caso real do log de 04/09/2026, na Proactor do Windows.
        ConnectionResetError(10054, "Foi forçado o cancelamento de uma conexão existente"),
        ConnectionAbortedError(10053, "conexão abortada pelo software do host"),
        BrokenPipeError(32, "broken pipe"),      # o equivalente no Linux
    ], ids=["reset-10054", "abort-10053", "broken-pipe"])
    def test_queda_de_peer_nao_chega_ao_handler(self, asyncio_log, exc):
        lg, capturados = asyncio_log
        _logar_excecao(lg, exc)
        assert capturados == []

    def test_descarta_em_debug_tambem(self, asyncio_log):
        """Diferente do `_FiltroPolling`, DEBUG não traz a linha de volta — e é o nível em
        que o posto estava rodando quando o ruído foi medido, então gatear por nível teria
        entregado um filtro que não filtra nada na máquina que motivou o filtro."""
        lg, capturados = asyncio_log
        lg.setLevel(logging.DEBUG)
        logging.getLogger().setLevel(logging.DEBUG)
        _logar_excecao(lg, ConnectionResetError(10054, "peer desligou"))
        assert capturados == []


def test_instalador_pendura_o_filtro_no_logger_asyncio():
    """`_silenciar_queda_de_peer` mira o logger `asyncio` — o nome usado por
    `asyncio/log.py` (`getLogger(__package__)`). Errar o nome daria um filtro que nunca
    roda, e nenhum teste de comportamento acima pegaria isso."""
    lg = logging.getLogger("asyncio")
    antes = list(lg.filters)
    try:
        _silenciar_queda_de_peer()
        assert any(isinstance(f, _FiltroQuedaDePeer) for f in lg.filters)
    finally:
        lg.filters = antes
