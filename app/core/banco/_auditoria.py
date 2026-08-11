"""Log de auditoria (quem fez o quê) e tokens de reset/convite de senha."""
from __future__ import annotations
import secrets
import time

from ._base import _agora

from ._base import cursor


# ─── Auditoria ───────────────────────────────────────────────────────────────

def auditoria_registrar(*, usuario_id: int | None, usuario_nome: str, acao: str,
                         alvo_tipo: str = "", alvo_id: str = "", detalhe: str = "") -> int:
    with cursor() as c:
        cur = c.execute(
            "INSERT INTO auditoria (criado_em, usuario_id, usuario_nome, acao, alvo_tipo, "
            "alvo_id, detalhe) VALUES (?,?,?,?,?,?,?)",
            (_agora(), usuario_id, usuario_nome, acao, alvo_tipo, str(alvo_id), detalhe),
        )
        return cur.lastrowid


def auditoria_listar(limit: int = 100, offset: int = 0, acao: str | None = None,
                      usuario_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM auditoria WHERE 1=1"
    params: list = []
    if acao:
        sql += " AND acao = ?"
        params.append(acao)
    if usuario_id is not None:
        sql += " AND usuario_id = ?"
        params.append(usuario_id)
    sql += " ORDER BY criado_em DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with cursor() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


# ─── Tokens de reset/convite de senha ────────────────────────────────────────

_TOKEN_TTL_SEG = 2 * 60 * 60  # 2 horas — link de e-mail não deve durar dias


def reset_token_criar(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with cursor() as c:
        c.execute(
            "INSERT INTO reset_senha_tokens (token, user_id, criado_em, expira_em, usado) "
            "VALUES (?,?,?,?,0)",
            (token, user_id, _agora(), time.time() + _TOKEN_TTL_SEG),
        )
    return token


def reset_token_resolver(token: str) -> dict | None:
    """None se o token não existe, já foi usado, ou expirou — não distingue os três
    casos pra quem chama (a mensagem pro usuário final é a mesma: "link inválido ou
    expirado, peça um novo"), evitando dar pista sobre qual é o caso."""
    with cursor() as c:
        r = c.execute(
            "SELECT * FROM reset_senha_tokens WHERE token=?", (token,)
        ).fetchone()
    if r is None or r["usado"] or time.time() > r["expira_em"]:
        return None
    return dict(r)


def reset_token_marcar_usado(token: str) -> None:
    with cursor() as c:
        c.execute("UPDATE reset_senha_tokens SET usado=1 WHERE token=?", (token,))


def reset_tokens_limpar_expirados(agora: float) -> int:
    with cursor() as c:
        return c.execute(
            "DELETE FROM reset_senha_tokens WHERE expira_em < ? OR usado=1", (agora,)
        ).rowcount
