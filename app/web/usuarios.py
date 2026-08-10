"""Gestão de usuários do painel (papel 'admin' × 'cliente' preso a um posto).

Router inteiro exige admin — dependency aplicada no include_router (app/servidor.py),
não rota a rota, porque não existe aqui NENHUMA ação que um 'cliente' deva poder fazer
(nem ver a lista dos outros usuários).
"""
from __future__ import annotations
import sqlite3

from fastapi import APIRouter, HTTPException, Request
from fastapi.templating import Jinja2Templates

from app.core import banco
from app.seguranca import sessao as auth_mod
from app.web import deps

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")

_PAPEIS_VALIDOS = ("admin", "cliente")


def _sem_senha(user: dict) -> dict:
    return {k: v for k, v in user.items() if k != "senha"}


@router.get("/usuarios")
def pagina(request: Request):
    return templates.TemplateResponse(request, "usuarios.html", {"usuario": deps.usuario_atual(request)})


@router.get("/api/usuarios")
def listar():
    return banco.usuarios_listar()


@router.get("/api/usuarios/eu")
def eu(request: Request):
    """Quem está logado — usado pela UI pra saber o próprio nome/papel sem repetir a
    lógica de sessão, e pelas travas de autoproteção abaixo (não mexer no próprio
    admin). 404 na leitura via api_key: não é uma sessão de usuário, não tem "eu"."""
    usuario = deps.usuario_atual(request)
    if usuario is None:
        raise HTTPException(404, "Sem sessão de usuário (acesso via api_key)")
    return _sem_senha(usuario)


@router.post("/api/usuarios")
def criar(payload: dict):
    nome = (payload.get("nome") or "").strip()
    email = (payload.get("email") or "").strip().lower()
    senha = payload.get("senha") or ""
    papel = payload.get("papel") or "cliente"
    empresa_id = payload.get("empresa_id")

    if not nome or not email:
        raise HTTPException(400, "nome e email são obrigatórios")
    if papel not in _PAPEIS_VALIDOS:
        raise HTTPException(400, f"papel deve ser um de {_PAPEIS_VALIDOS}")
    if len(senha) < 8:
        raise HTTPException(400, "senha deve ter pelo menos 8 caracteres")
    if papel == "cliente":
        if not empresa_id:
            raise HTTPException(400, "empresa_id é obrigatório para papel 'cliente'")
        if not banco.empresas_obter(int(empresa_id)):
            raise HTTPException(400, f"Empresa {empresa_id} não encontrada")

    uid = banco.criar_usuario(
        nome, email, auth_mod.hash_senha(senha), papel=papel,
        empresa_id=int(empresa_id) if empresa_id else None,
    )
    if uid is None:
        raise HTTPException(409, f"E-mail {email} já cadastrado")
    return {"id": uid}


@router.put("/api/usuarios/{id_}")
def atualizar(id_: int, payload: dict, request: Request):
    atual = banco.buscar_usuario_id(id_)
    if not atual:
        raise HTTPException(404, "Usuário não encontrado")

    nome = (payload.get("nome") or atual["nome"]).strip()
    email = (payload.get("email") or atual["email"]).strip().lower()
    papel = payload.get("papel") or "cliente"
    empresa_id = payload.get("empresa_id")
    ativo = bool(payload.get("ativo", True))
    senha = payload.get("senha") or ""

    if papel not in _PAPEIS_VALIDOS:
        raise HTTPException(400, f"papel deve ser um de {_PAPEIS_VALIDOS}")
    if papel == "cliente":
        if not empresa_id:
            raise HTTPException(400, "empresa_id é obrigatório para papel 'cliente'")
        if not banco.empresas_obter(int(empresa_id)):
            raise HTTPException(400, f"Empresa {empresa_id} não encontrada")
    if senha and len(senha) < 8:
        raise HTTPException(400, "senha deve ter pelo menos 8 caracteres")

    # Autoproteção: ninguém mexe no PRÓPRIO status administrativo por aqui — mesmo que
    # sobrem outros admins. É uma trava a mais além de "não pode ser o último admin"
    # (abaixo): sem ela, um admin distraído se rebaixa ou se desativa sozinho e precisa
    # de OUTRO admin pra desfazer, o que é evitável de graça.
    quem_pede = deps.usuario_atual(request)
    eh_auto_edicao = quem_pede is not None and quem_pede["id"] == id_
    if eh_auto_edicao and atual["papel"] == "admin" and (papel != "admin" or not ativo):
        raise HTTPException(400, "Você não pode alterar seu próprio papel ou status — peça a outro administrador.")

    vira_nao_admin = atual["papel"] == "admin" and (papel != "admin" or not ativo)
    if vira_nao_admin and banco.usuarios_contar_admins_ativos(excluir_id=id_) == 0:
        raise HTTPException(
            400,
            "Este é o último administrador ativo — promova outro usuário a admin "
            "antes de rebaixar ou desativar este.",
        )

    dados = {
        "nome": nome, "email": email,
        "papel": papel, "empresa_id": int(empresa_id) if empresa_id else None,
        "ativo": ativo,
    }
    if senha:
        dados["senha_hash"] = auth_mod.hash_senha(senha)

    try:
        banco.usuarios_atualizar(id_, dados)
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"E-mail {email} já cadastrado")

    # Sessões: qualquer coisa que mude o que essa conta PODE fazer (senha, papel,
    # desativação) precisa valer imediatamente em todo navegador aberto, não só em
    # logins novos. `exceto_token` preserva a sessão de quem está editando A SI MESMO
    # (ex.: trocar a própria senha) — só possível aqui porque a autoproteção acima já
    # garante que papel/ativo não mudaram nesse caso.
    mudou_algo_sensivel = not ativo or papel != atual["papel"] or bool(senha)
    if mudou_algo_sensivel:
        token_atual = request.cookies.get("sessao") if eh_auto_edicao else None
        auth_mod.remover_sessoes_usuario(id_, exceto_token=token_atual)

    return {"atualizado": True}


@router.post("/api/usuarios/{id_}/senha")
def redefinir_senha(id_: int, payload: dict, request: Request):
    senha = payload.get("senha") or ""
    if len(senha) < 8:
        raise HTTPException(400, "senha deve ter pelo menos 8 caracteres")
    if not banco.usuarios_definir_senha(id_, auth_mod.hash_senha(senha)):
        raise HTTPException(404, "Usuário não encontrado")
    # Redefinir senha é tipicamente resposta a "acho que vazou" — de nada adianta
    # trocar a senha e deixar a sessão antiga (cookie já vazado) continuar valendo.
    # Exceção: a própria sessão de quem está redefinindo AGORA (self-service).
    quem_pede = deps.usuario_atual(request)
    eh_auto_edicao = quem_pede is not None and quem_pede["id"] == id_
    token_atual = request.cookies.get("sessao") if eh_auto_edicao else None
    auth_mod.remover_sessoes_usuario(id_, exceto_token=token_atual)
    return {"redefinida": True}
