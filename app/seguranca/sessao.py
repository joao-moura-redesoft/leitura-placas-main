"""Autenticação: hash de senhas (bcrypt) e sessões persistidas no banco.

As sessões viviam num dicionário em memória. Isso significava que todo restart do
servidor deslogava todo mundo — mesmo com o cookie ainda válido por dias no navegador,
o que produzia o "cookie morto" que ninguém conseguia limpar — e impedia rodar o uvicorn
com mais de um worker, porque cada processo teria seu próprio dicionário e o login só
valeria no worker que atendeu o POST. Agora ficam na tabela `sessoes`.
"""
from __future__ import annotations
import secrets
import threading
import time

import bcrypt

from app.core import banco

_SESSION_TTL = 60 * 60 * 24 * 7   # 7 dias — bate com o max_age do cookie (auth.py)
_CLEANUP_INTERVAL = 300           # limpeza a cada 5 minutos

# A sessão só é prorrogada quando já gastou esta fração do TTL. Antes a renovação era
# um write em dicionário e podia rodar a cada request; agora é um UPDATE no SQLite, e
# fazer isso em toda request desperdiçaria uma escrita por page view sem ganho nenhum
# — meio dia de folga é indistinguível de 7 dias para quem está usando o sistema.
_RENOVAR_APOS = 0.5

# Custo do bcrypt (2^n rodadas). 12 é o valor de produção: ~0,2s por hash, caro o
# bastante para atrapalhar força bruta offline sem pesar no login. Fica nomeado aqui
# para a suíte de testes poder baixá-lo — com o custo real, os testes que criam
# usuário gastariam a maior parte do tempo derivando hash que ninguém vai atacar.
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


def usuario_autenticado(token: str | None) -> dict | None:
    """Usuário dono deste token — só se a sessão estiver viva E a conta ainda existir
    e estiver ativa.

    Resolver só o `user_id` não bastava: a sessão não sabia nada do banco, então remover
    ou desativar um usuário não derrubava o acesso dele — o token continuava valendo, e
    se renovando a cada uso, por dias. Aqui a sessão é confrontada com o estado atual da
    conta a cada request e morre junto com ela.
    """
    if not token:
        return None
    reg = banco.sessao_resolver(token)
    if reg is None:
        return None

    expira_em = reg.pop("expira_em")
    agora = time.time()
    if agora > expira_em or not reg["ativo"]:
        banco.sessao_remover(token)
        return None

    if expira_em - agora < _SESSION_TTL * _RENOVAR_APOS:
        banco.sessao_renovar(token, agora + _SESSION_TTL)
    return reg


def obter_user_id(token: str) -> int | None:
    user = usuario_autenticado(token)
    return user["id"] if user else None


def remover_sessao(token: str) -> None:
    banco.sessao_remover(token)


def remover_sessoes_do_usuario(user_id: int, exceto_token: str | None = None) -> int:
    """Desconecta o usuário de todos os navegadores — usado ao desativar a conta,
    rebaixar o papel ou trocar a senha, para a mudança valer na hora e não só no
    próximo login. `exceto_token` poupa a sessão de quem fez a alteração."""
    return banco.sessoes_remover_do_usuario(user_id, exceto_token=exceto_token)


def limpar_sessoes_expiradas() -> int:
    return banco.sessoes_limpar_expiradas(time.time())


def _cleanup_loop() -> None:
    from app.seguranca import tentativas
    while True:
        time.sleep(_CLEANUP_INTERVAL)
        try:
            limpar_sessoes_expiradas()
            tentativas.limpar_expiradas()
        except Exception:
            pass


def iniciar_cleanup() -> None:
    """Inicia thread de limpeza de sessões expiradas e contadores de força bruta.
    Chamar uma vez no startup."""
    threading.Thread(target=_cleanup_loop, daemon=True, name="session-cleanup").start()
