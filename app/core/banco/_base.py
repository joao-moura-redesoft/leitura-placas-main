"""Conexão com o SQLite e utilidades compartilhadas pelos módulos de dados."""
from __future__ import annotations
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# Configurável por ambiente (mesmo padrão de config.CONFIG_PATH). Em container, aponta
# para um DIRETÓRIO montado como volume — nunca para um arquivo montado sozinho: com
# journal_mode=WAL o SQLite cria `placas.db-wal`/`placas.db-shm` AO LADO do banco, e um
# bind mount de arquivo único deixa esses dois no filesystem efêmero do container.
# Recriar o container descartaria o -wal com transações ainda não integradas = escritas
# perdidas silenciosamente.
_DB_PATH_PADRAO = Path(os.environ.get("DB_PATH", "placas.db"))
_db_path: Path = _DB_PATH_PADRAO


def caminho() -> Path:
    """Arquivo do banco em uso."""
    return _db_path


def definir_caminho(novo: Path | str) -> None:
    """Aponta o banco para outro arquivo e descarta a conexão desta thread.

    Existe para os testes, que dão um banco próprio a cada caso: sem soltar a conexão
    junto, a thread continuaria falando com o arquivo anterior.
    """
    global _db_path
    _db_path = Path(novo)
    fechar_conexao()


def _abrir() -> sqlite3.Connection:
    c = sqlite3.connect(_db_path, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    # NORMAL é seguro com WAL (só FULL protege contra corrupção do SO travando no
    # meio de um fsync, cenário que FULL não evita de qualquer forma) e evita um
    # fsync por commit — relevante aqui porque toda leitura reativa grava em
    # `chamadas`/`deteccoes` no caminho crítico de latência.
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


# Conexão por THREAD, reaproveitada entre operações.
#
# Antes cada `cursor()` abria e fechava uma conexão nova, pagando o open do arquivo
# mais três PRAGMAs a cada consulta — e agora toda request autenticada faz pelo menos
# uma (resolver a sessão), com o dashboard fazendo polling por cima. Uma conexão do
# sqlite3 não pode cruzar threads, então o cache é thread-local: as threads de câmera,
# o worker de retenção e as threads do servidor ficam cada uma com a sua.
_local = threading.local()


def conexao() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = _local.conn = _abrir()
    return c


def fechar_conexao() -> None:
    """Fecha a conexão desta thread, se houver. Para threads de vida curta e para os
    testes, que trocam o DB_PATH entre casos e não podem herdar a conexão anterior."""
    c = getattr(_local, "conn", None)
    if c is not None:
        _local.conn = None
        c.close()


@contextmanager
def cursor():
    c = conexao()
    try:
        yield c
        c.commit()
    except BaseException:
        # A conexão é reusada pela thread: sem o rollback, uma transação abortada
        # continuaria aberta e o próximo commit desta thread gravaria junto o que
        # sobrou da operação que falhou.
        c.rollback()
        raise


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat()


def inicio_do_dia_local(fuso: str = "America/Sao_Paulo") -> str:
    """Meia-noite de HOJE no fuso do posto, em ISO-8601 UTC — pronto para comparar com
    `criado_em`.

    Existe porque `date('now')` do SQLite é UTC: num posto em BRT (UTC-3), o contador de
    "hoje" zerava às 21h locais e incluía as leituras das 21h-24h da véspera. O
    armazenamento continua em UTC; o que muda é onde o dia começa.
    """
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(fuso)
    except Exception:
        # Fuso inválido no config não pode derrubar o dashboard: cai para UTC, que é o
        # comportamento antigo, e quem olhar o número vê no máximo o deslocamento de antes.
        tz = timezone.utc
    agora_local = datetime.now(timezone.utc).astimezone(tz)
    meia_noite = agora_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return meia_noite.astimezone(timezone.utc).isoformat()


def _normalizar_codigo(codigo: str) -> str:
    """Tolera diferença de espaço/maiúscula no código vindo do roteador.

    O código não tem significado numérico — é só um rótulo opaco — então "1", " 1 " e
    "1 " são o mesmo bico/automação para qualquer humano. Como o lado que envia (roteador
    Java) é integração nova, é o tipo de diferença boba mais provável de acontecer.
    """
    return (codigo or "").strip().upper()

