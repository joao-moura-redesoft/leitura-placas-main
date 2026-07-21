"""Autenticação: hash de senhas (bcrypt) e sessões em memória com TTL."""
from __future__ import annotations
import secrets
import threading
import time

import bcrypt

_SESSION_TTL = 60 * 60       # 1 hora
_CLEANUP_INTERVAL = 300      # limpeza a cada 5 minutos

# token → (user_id, expires_at)
_sessions: dict[str, tuple[int, float]] = {}
_lock = threading.Lock()


def hash_senha(senha: str) -> str:
    return bcrypt.hashpw(senha.encode(), bcrypt.gensalt(12)).decode()


def verificar_senha(senha: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(senha.encode(), hashed.encode())
    except Exception:
        return False


def criar_sessao(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with _lock:
        _sessions[token] = (user_id, time.time() + _SESSION_TTL)
    return token


def obter_user_id(token: str) -> int | None:
    agora = time.time()
    with _lock:
        entry = _sessions.get(token)
        if entry is None:
            return None
        user_id, expires_at = entry
        if agora > expires_at:
            del _sessions[token]
            return None
        _sessions[token] = (user_id, agora + _SESSION_TTL)  # renova a cada uso
        return user_id


def remover_sessao(token: str) -> None:
    with _lock:
        _sessions.pop(token, None)


def limpar_sessoes_expiradas() -> int:
    agora = time.time()
    with _lock:
        expiradas = [t for t, (_, exp) in _sessions.items() if agora > exp]
        for t in expiradas:
            del _sessions[t]
    return len(expiradas)


def _cleanup_loop() -> None:
    while True:
        time.sleep(_CLEANUP_INTERVAL)
        try:
            limpar_sessoes_expiradas()
        except Exception:
            pass


def iniciar_cleanup() -> None:
    """Inicia thread de limpeza de sessões expiradas. Chamar uma vez no startup."""
    threading.Thread(target=_cleanup_loop, daemon=True, name="session-cleanup").start()
