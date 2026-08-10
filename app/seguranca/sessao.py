"""Autenticação: hash de senhas (bcrypt) e sessões — persistidas em SQLite
(app/core/banco.py: `sessao_*`), não mais num dict em memória do processo.

Por quê: sessões em memória não sobrevivem a um restart do servidor (todo mundo
deslogado de repente, mesmo com o cookie ainda válido no navegador) e não são
compartilháveis entre processos — travava a opção de rodar múltiplos workers uvicorn
pra escalar horizontalmente (ver docs/ARQUITETURA.md §20, capacidade/escala). Persistir
em SQLite resolve as duas coisas de graça, reaproveitando a mesma conexão que o resto
do banco já usa. A interface pública deste módulo não mudou — quem chama
`criar_sessao`/`obter_user_id`/etc. não precisa saber que a implementação trocou.
"""
from __future__ import annotations
import secrets
import threading
import time

import bcrypt

from app.core import banco
from app.seguranca import limitador
from app.seguranca import tentativas

_SESSION_TTL = 60 * 60       # 1 hora
_CLEANUP_INTERVAL = 300      # limpeza a cada 5 minutos

# Custo do bcrypt — módulo-nível (não parâmetro de função) para a suíte de testes poder
# baixá-lo via `monkeypatch.setattr(auth_mod, "_BCRYPT_ROUNDS", 4)` (ver
# testes/unitarios/conftest.py): no custo de produção, cada usuário criado numa fixture
# custaria ~0,2s e a suíte passaria a maior parte do tempo derivando hash à toa.
_BCRYPT_ROUNDS = 12


def hash_senha(senha: str) -> str:
    return bcrypt.hashpw(senha.encode(), bcrypt.gensalt(_BCRYPT_ROUNDS)).decode()


def verificar_senha(senha: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(senha.encode(), hashed.encode())
    except Exception:
        return False


def criar_sessao(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    banco.sessao_criar(token, user_id, time.time() + _SESSION_TTL)
    return token


def obter_user_id(token: str) -> int | None:
    """None se o token não existe, expirou, ou a conta foi desativada nesse meio
    tempo. `sessao_resolver` não julga nada disso sozinha (ver a docstring dela em
    app/core/banco/_acesso.py) — é aqui que a validade é decidida."""
    sessao = banco.sessao_resolver(token)
    if sessao is None:
        return None
    if time.time() > sessao["expira_em"]:
        banco.sessao_remover(token)
        return None
    if not sessao["ativo"]:
        return None
    banco.sessao_renovar(token, time.time() + _SESSION_TTL)  # renova a cada uso
    return sessao["id"]


def remover_sessao(token: str) -> None:
    banco.sessao_remover(token)


def remover_sessoes_usuario(user_id: int, exceto_token: str | None = None) -> int:
    """Derruba as sessões ativas de um usuário — chamado ao redefinir a senha dele,
    desativá-lo ou trocar o papel, pra mudança valer imediatamente em todo navegador
    aberto, não só em logins novos.

    `exceto_token` preserva a sessão de quem está fazendo a PRÓPRIA alteração: trocar
    a própria senha não deve expulsar você da tela em que acabou de trocá-la."""
    return banco.sessoes_remover_do_usuario(user_id, exceto_token=exceto_token)


def limpar_sessoes_expiradas() -> int:
    return banco.sessoes_limpar_expiradas(time.time())


def _cleanup_loop() -> None:
    while True:
        time.sleep(_CLEANUP_INTERVAL)
        try:
            limpar_sessoes_expiradas()
        except Exception:
            pass
        try:
            limitador.limpar_antigos()
        except Exception:
            pass
        try:
            tentativas.limpar_expiradas()
        except Exception:
            pass


def iniciar_cleanup() -> None:
    """Inicia thread de limpeza de sessões expiradas. Chamar uma vez no startup."""
    threading.Thread(target=_cleanup_loop, daemon=True, name="session-cleanup").start()
