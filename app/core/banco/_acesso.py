"""Usuários e sessões de login."""
from __future__ import annotations
import sqlite3

from ._base import _agora

from ._base import cursor


# ─── Usuários ──────────────────────────────────────────────────────────────

def contar_usuarios() -> int:
    with cursor() as c:
        return c.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]


def criar_usuario(nome: str, email: str, senha_hash: str, papel: str = "admin", ativo: int = 1) -> int | None:
    """None = e-mail já cadastrado. Só o conflito de UNIQUE vira None: engolir todo
    `Exception` fazia disco cheio ou banco travado se passarem por "e-mail duplicado",
    escondendo a falha real de quem estivesse cadastrando."""
    try:
        with cursor() as c:
            cur = c.execute(
                "INSERT INTO usuarios (nome, email, senha, papel, ativo, criado_em) VALUES (?,?,?,?,?,?)",
                (nome, email, senha_hash, papel, ativo, _agora()),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def buscar_usuario_email(email: str) -> dict | None:
    with cursor() as c:
        r = c.execute("SELECT * FROM usuarios WHERE email=?", (email.strip().lower(),)).fetchone()
        return dict(r) if r else None


def buscar_usuario_id(id_: int) -> dict | None:
    with cursor() as c:
        r = c.execute("SELECT * FROM usuarios WHERE id=?", (id_,)).fetchone()
        return dict(r) if r else None


def usuarios_listar() -> list[dict]:
    # Sem a coluna `senha` — hash nunca deve sair numa resposta de listagem.
    with cursor() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, nome, email, papel, ativo, criado_em FROM usuarios ORDER BY nome"
        ).fetchall()]


def usuarios_atualizar(id_: int, dados: dict) -> bool:
    """Atualiza nome/email/papel/ativo. Senha só é trocada quando `senha_hash` vem
    preenchido — campo vazio no formulário significa "manter a senha atual", mesmo
    padrão usado em `cameras_atualizar` para não sobrescrever com string vazia."""
    senha_hash = dados.get("senha_hash")
    with cursor() as c:
        if senha_hash:
            cur = c.execute(
                "UPDATE usuarios SET nome=?, email=?, papel=?, ativo=?, senha=? WHERE id=?",
                (dados["nome"], dados["email"], dados["papel"], dados["ativo"], senha_hash, id_),
            )
        else:
            cur = c.execute(
                "UPDATE usuarios SET nome=?, email=?, papel=?, ativo=? WHERE id=?",
                (dados["nome"], dados["email"], dados["papel"], dados["ativo"], id_),
            )
        return cur.rowcount > 0


def usuarios_remover(id_: int) -> bool:
    with cursor() as c:
        cur = c.execute("DELETE FROM usuarios WHERE id=?", (id_,))
        return cur.rowcount > 0


# ─── Sessões de login ──────────────────────────────────────────────────────

def sessao_criar(token: str, user_id: int, expira_em: float) -> None:
    with cursor() as c:
        c.execute("INSERT INTO sessoes (token, user_id, criado_em, expira_em) VALUES (?,?,?,?)",
                  (token, user_id, _agora(), expira_em))


def sessao_resolver(token: str) -> dict | None:
    """Sessão + dono numa consulta só — é o caminho quente (toda request autenticada
    passa por aqui). Devolve as colunas do usuário mais `expira_em`, sem o hash da senha.
    Não julga validade: quem chama decide sobre expiração e conta desativada."""
    with cursor() as c:
        r = c.execute(
            "SELECT u.id, u.nome, u.email, u.papel, u.ativo, u.criado_em, s.expira_em "
            "FROM sessoes s JOIN usuarios u ON u.id = s.user_id WHERE s.token=?",
            (token,),
        ).fetchone()
        return dict(r) if r else None


def sessao_renovar(token: str, expira_em: float) -> None:
    with cursor() as c:
        c.execute("UPDATE sessoes SET expira_em=? WHERE token=?", (expira_em, token))


def sessao_remover(token: str) -> None:
    with cursor() as c:
        c.execute("DELETE FROM sessoes WHERE token=?", (token,))


def sessoes_remover_do_usuario(user_id: int, exceto_token: str | None = None) -> int:
    """Derruba as sessões de um usuário — usado ao desativar a conta, rebaixar o papel
    ou trocar a senha, para a mudança valer imediatamente em todos os navegadores.

    `exceto_token` preserva a sessão de quem está fazendo a alteração: trocar a própria
    senha não deve expulsar você da tela em que acabou de trocá-la."""
    sql = "DELETE FROM sessoes WHERE user_id=?"
    params: list = [user_id]
    if exceto_token:
        sql += " AND token<>?"
        params.append(exceto_token)
    with cursor() as c:
        return c.execute(sql, params).rowcount


def sessoes_limpar_expiradas(agora: float) -> int:
    with cursor() as c:
        return c.execute("DELETE FROM sessoes WHERE expira_em < ?", (agora,)).rowcount


def usuarios_contar_admins_ativos(excluir_id: int | None = None) -> int:
    """Usado para travar a última conta de administrador ativa — sem isso, removê-la
    ou rebaixá-la trancaria todo mundo fora do próprio painel de usuários."""
    sql = "SELECT COUNT(*) FROM usuarios WHERE papel='admin' AND ativo=1"
    params: list = []
    if excluir_id is not None:
        sql += " AND id<>?"
        params.append(excluir_id)
    with cursor() as c:
        return c.execute(sql, params).fetchone()[0]
