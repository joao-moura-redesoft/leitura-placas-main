"""O vigia da porta — a thread que derruba o processo quando o painel para de atender.

Existe porque o modo de falha real deste servidor não é o processo morrer, é ele continuar
vivo sem atender: em 04/09/2026 o socket de escuta sumiu às 10:35 e o processo seguiu de pé
por mais de uma hora, lendo placa e gravando no banco, com `localhost:14000` recusando
conexão. Tudo que vigiava câmera estava saudável; ninguém vigiava a porta.

Um vigia que derruba processo é código perigoso: os dois erros possíveis são graves e
opostos. Se disparar cedo demás, mata o servidor durante o boot (modelos aquecendo,
primeira câmera conectando) e o sistema nunca sobe; se não disparar, volta a falha que ele
existe para cobrir. Este arquivo tranca os dois lados.
"""
from __future__ import annotations

import socket
import threading

import pytest

from app import servidor


# ── A sonda ─────────────────────────────────────────────────────────────────

class _ServidorDeMentira:
    """Servidor HTTP mínimo num thread: responde o que lhe mandarem responder."""

    def __init__(self, resposta: bytes) -> None:
        self.resposta = resposta
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.porta = self.sock.getsockname()[1]
        self._parar = False
        self.thread = threading.Thread(target=self._servir, daemon=True)
        self.thread.start()

    def _servir(self) -> None:
        while not self._parar:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.recv(1024)
                    conn.sendall(self.resposta)
                except OSError:
                    pass

    def fechar(self) -> None:
        self._parar = True
        self.sock.close()


def _porta_sem_ninguem() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_sonda_ve_200():
    srv = _ServidorDeMentira(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
    try:
        assert servidor._healthz_responde(srv.porta, timeout=3) is True
    finally:
        srv.fechar()


def test_sonda_nao_confunde_500_com_saude():
    """O vigia pergunta se o servidor ATENDE, e 500 é o servidor dizendo que não está
    bem. Aceitar qualquer resposta HTTP transformaria o vigia num teste de TCP."""
    srv = _ServidorDeMentira(b"HTTP/1.1 500 Internal Server Error\r\n\r\n")
    try:
        assert servidor._healthz_responde(srv.porta, timeout=3) is False
    finally:
        srv.fechar()


def test_sonda_ve_porta_fechada():
    assert servidor._healthz_responde(_porta_sem_ninguem(), timeout=1) is False


def test_sonda_e_http_e_nao_um_connect_seco():
    """Porta que aceita conexão e não responde nada = loop travado. Um `connect` seco diria
    "saudável" (o sistema operacional aceita pelo backlog); a sonda tem de dizer não."""
    srv = _ServidorDeMentira(b"")
    try:
        assert servidor._healthz_responde(srv.porta, timeout=2) is False
    finally:
        srv.fechar()


# ── A decisão de derrubar ───────────────────────────────────────────────────

@pytest.fixture
def vigia_rapido(monkeypatch):
    """Vigia com cadência de teste, `_sair` desarmado, e PARADO no teardown.

    Parar no teardown não é higiene opcional: um vigia sobrevivente continua sondando a
    cada 0,01 s, consome a fila de respostas falsas do teste SEGUINTE e rouba CPU de testes
    que medem paralelismo. Medido — a primeira versão deste arquivo derrubou três testes na
    suíte completa, dois deles em arquivos sem nenhuma relação com o vigia.
    """
    saidas: list[int] = []
    paradas: list[threading.Event] = []
    monkeypatch.setattr(servidor, "INTERVALO_VIGIA_SEG", 0.01)
    monkeypatch.setattr(servidor, "_sair", lambda codigo: saidas.append(codigo))

    original = servidor._iniciar_vigia_da_porta

    def rastreando(porta):
        t, parar = original(porta)
        paradas.append(parar)
        return t, parar

    monkeypatch.setattr(servidor, "_iniciar_vigia_da_porta", rastreando)
    yield saidas
    for parar in paradas:
        parar.set()


def _sondas(monkeypatch, respostas: list[bool]) -> threading.Event:
    """Faz a sonda devolver `respostas` em ordem (e repetir a última). Avisa quando acabam."""
    fim = threading.Event()
    estado = {"i": 0}

    def falsa(porta, timeout=5.0):
        i = min(estado["i"], len(respostas) - 1)
        estado["i"] += 1
        if estado["i"] >= len(respostas):
            fim.set()
        return respostas[i]

    monkeypatch.setattr(servidor, "_healthz_responde", falsa)
    return fim


def test_nao_derruba_antes_do_primeiro_sucesso(monkeypatch, vigia_rapido):
    """O caso perigoso: durante o boot a porta ainda não existe. O vigia tem de esperar,
    não matar. Sem isto, o servidor nunca chegaria a subir numa máquina lenta."""
    fim = _sondas(monkeypatch, [False] * 30)
    servidor._iniciar_vigia_da_porta(4242)
    assert fim.wait(10), "a sonda não foi chamada as vezes esperadas"
    assert vigia_rapido == [], "o vigia derrubou o processo antes de a porta existir"


def test_derruba_depois_de_falhas_seguidas_com_a_porta_ja_vista(monkeypatch, vigia_rapido):
    ok = [True]
    quedas = [False] * (servidor.FALHAS_VIGIA_PARA_CAIR + 2)
    fim = _sondas(monkeypatch, ok + quedas)
    servidor._iniciar_vigia_da_porta(4242)
    assert fim.wait(10), "a sonda não foi chamada as vezes esperadas"
    assert 1 in vigia_rapido, (
        "o painel ficou fora do ar e o vigia não derrubou o processo — é a falha de "
        "04/09/2026 de volta"
    )


def test_falha_isolada_nao_derruba(monkeypatch, vigia_rapido):
    """Uma sonda que falha e volta é pico de carga, não servidor morto. Derrubar aí seria
    trocar um painel lento por um painel que reinicia sozinho no meio do movimento."""
    padrao = ([True] + [False] * (servidor.FALHAS_VIGIA_PARA_CAIR - 1)) * 6
    fim = _sondas(monkeypatch, padrao)
    servidor._iniciar_vigia_da_porta(4242)
    assert fim.wait(10), "a sonda não foi chamada as vezes esperadas"
    assert vigia_rapido == [], "falha isolada derrubou o processo"


def test_a_thread_e_daemon():
    """Se o vigia não for daemon ele vira mais um motivo para o processo não morrer — o
    oposto exato do que ele existe para fazer."""
    t, parar = servidor._iniciar_vigia_da_porta(_porta_sem_ninguem())
    try:
        assert t.daemon
    finally:
        parar.set()


def test_parar_encerra_o_vigia():
    """O `Event` é o que o desligamento usa para calar o vigia antes de fechar a porta de
    propósito — sem ele, o vigia derrubaria o processo no meio de um desligamento normal."""
    t, parar = servidor._iniciar_vigia_da_porta(_porta_sem_ninguem())
    parar.set()
    # `parar.wait()` acorda na hora, então o join não espera o intervalo inteiro — a folga
    # é só para máquina carregada.
    t.join(timeout=servidor.INTERVALO_VIGIA_SEG + 5)
    assert not t.is_alive(), "o vigia não morreu depois de `parar.set()`"
