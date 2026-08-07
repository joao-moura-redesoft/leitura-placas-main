"""CRUD administrativo de usuários — restrito a quem tem papel 'admin'.

Mesmo padrão de validação/erros do CRUD de cadastro multi-tenant (app/web/cadastro.py).
"""
from __future__ import annotations
import sqlite3

from fastapi import APIRouter, HTTPException, Request

from app.core import banco
from app.seguranca import sessao as auth_mod

router = APIRouter(prefix="/api")

_PAPEIS_VALIDOS = ("admin", "usuario")


def usuario_atual(request: Request) -> dict | None:
    """Usuário logado nesta request.

    O `_AuthMiddleware` já resolveu a sessão e deixou o resultado em `request.state`;
    reusar dali evita uma segunda consulta ao banco por request. O fallback cobre quem
    chegar aqui sem passar pelo middleware (teste de unidade chamando a rota direto).
    """
    usuario = getattr(request.state, "usuario", ...)
    if usuario is not ...:
        return usuario
    return auth_mod.usuario_autenticado(request.cookies.get("sessao"))


def _exigir_admin(request: Request) -> dict:
    user = usuario_atual(request)
    if not user or user.get("papel") != "admin":
        raise HTTPException(403, "Apenas administradores podem gerenciar usuários.")
    return user


def _sem_senha(u: dict) -> dict:
    return {k: v for k, v in u.items() if k != "senha"}


@router.get("/usuarios/eu")
def usuarios_eu(request: Request):
    """Usuário logado — a UI usa isto pra saber se mostra ações de administração."""
    user = usuario_atual(request)
    if not user:
        raise HTTPException(401, "Não autenticado.")
    return _sem_senha(user)


@router.get("/usuarios")
def usuarios_listar(request: Request):
    _exigir_admin(request)
    return banco.usuarios_listar()


@router.post("/usuarios")
def usuarios_inserir(payload: dict, request: Request):
    _exigir_admin(request)
    nome = (payload.get("nome") or "").strip()
    email = (payload.get("email") or "").strip().lower()
    senha = payload.get("senha") or ""
    papel = payload.get("papel") or "usuario"
    if not nome or not email:
        raise HTTPException(400, "nome e email são obrigatórios")
    if papel not in _PAPEIS_VALIDOS:
        raise HTTPException(400, "papel inválido")
    if len(senha) < 8:
        raise HTTPException(400, "A senha deve ter pelo menos 8 caracteres.")
    uid = banco.criar_usuario(nome, email, auth_mod.hash_senha(senha), papel=papel,
                               ativo=1 if payload.get("ativo", True) else 0)
    if uid is None:
        raise HTTPException(409, f"E-mail '{email}' já cadastrado")
    return {"id": uid}


@router.put("/usuarios/{id_}")
def usuarios_atualizar(id_: int, payload: dict, request: Request):
    admin_atual = _exigir_admin(request)
    alvo = banco.buscar_usuario_id(id_)
    if not alvo:
        raise HTTPException(404, "Usuário não encontrado")

    nome = (payload.get("nome") or "").strip()
    email = (payload.get("email") or "").strip().lower()
    papel = payload.get("papel") or alvo["papel"]
    ativo = 1 if payload.get("ativo", True) else 0
    if not nome or not email:
        raise HTTPException(400, "nome e email são obrigatórios")
    if papel not in _PAPEIS_VALIDOS:
        raise HTTPException(400, "papel inválido")

    # Sem estas duas travas dá pra um admin se autorrebaixar/desativar e ficar
    # trancado do próprio painel, ou remover o último admin ativo do sistema.
    perde_admin_ativo = papel != "admin" or not ativo
    if id_ == admin_atual["id"] and perde_admin_ativo:
        raise HTTPException(400, "Você não pode remover seu próprio acesso de administrador.")
    if alvo["papel"] == "admin" and perde_admin_ativo:
        if banco.usuarios_contar_admins_ativos(excluir_id=id_) == 0:
            raise HTTPException(400, "Deve haver ao menos um administrador ativo.")

    dados = {"nome": nome, "email": email, "papel": papel, "ativo": ativo}
    senha = payload.get("senha") or ""
    if senha:
        if len(senha) < 8:
            raise HTTPException(400, "A senha deve ter pelo menos 8 caracteres.")
        dados["senha_hash"] = auth_mod.hash_senha(senha)

    try:
        ok = banco.usuarios_atualizar(id_, dados)
    except sqlite3.IntegrityError as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, f"E-mail '{email}' já cadastrado")
        raise HTTPException(400, f"Referência inválida: {e}")
    if not ok:
        raise HTTPException(404, "Usuário não encontrado")

    # Desativar, rebaixar ou trocar a senha só vale de verdade se derrubar as sessões
    # já abertas. `usuario_autenticado` bloquearia a conta desativada na request
    # seguinte de qualquer jeito, mas um rebaixamento de admin→usuario não é conta
    # desativada: sem isto o navegador que já estava logado seguiria como admin.
    if senha or not ativo or papel != alvo["papel"]:
        auth_mod.remover_sessoes_do_usuario(id_, exceto_token=request.cookies.get("sessao"))
    return {"atualizado": True}


@router.delete("/usuarios/{id_}")
def usuarios_remover(id_: int, request: Request):
    admin_atual = _exigir_admin(request)
    alvo = banco.buscar_usuario_id(id_)
    if not alvo:
        raise HTTPException(404, "Usuário não encontrado")
    if id_ == admin_atual["id"]:
        raise HTTPException(400, "Você não pode remover seu próprio usuário.")
    if alvo["papel"] == "admin" and banco.usuarios_contar_admins_ativos(excluir_id=id_) == 0:
        raise HTTPException(400, "Deve haver ao menos um administrador ativo.")
    banco.usuarios_remover(id_)
    return {"removido": True}
