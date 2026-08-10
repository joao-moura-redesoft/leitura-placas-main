"""Gestão de usuários do painel (papel 'admin' × 'cliente' preso a um posto).

Router inteiro exige admin — dependency aplicada no include_router (app/servidor.py),
não rota a rota, porque não existe aqui NENHUMA ação que um 'cliente' deva poder fazer
(nem ver a lista dos outros usuários).
"""
from __future__ import annotations
from fastapi import APIRouter, HTTPException, Request
from fastapi.templating import Jinja2Templates

from app.core import banco
from app.seguranca import sessao as auth_mod
from app.web import deps

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")

_PAPEIS_VALIDOS = ("admin", "cliente")


@router.get("/usuarios")
def pagina(request: Request):
    return templates.TemplateResponse(request, "usuarios.html", {"usuario": deps.usuario_atual(request)})


@router.get("/api/usuarios")
def listar():
    return banco.usuarios_listar()


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
def atualizar(id_: int, payload: dict):
    atual = banco.buscar_usuario_id(id_)
    if not atual:
        raise HTTPException(404, "Usuário não encontrado")

    papel = payload.get("papel") or "cliente"
    empresa_id = payload.get("empresa_id")
    ativo = bool(payload.get("ativo", True))

    if papel not in _PAPEIS_VALIDOS:
        raise HTTPException(400, f"papel deve ser um de {_PAPEIS_VALIDOS}")
    if papel == "cliente":
        if not empresa_id:
            raise HTTPException(400, "empresa_id é obrigatório para papel 'cliente'")
        if not banco.empresas_obter(int(empresa_id)):
            raise HTTPException(400, f"Empresa {empresa_id} não encontrada")

    vira_nao_admin = atual["papel"] == "admin" and (papel != "admin" or not ativo)
    if vira_nao_admin and banco.usuarios_contar_admins_ativos(excluir_id=id_) == 0:
        raise HTTPException(
            400,
            "Este é o último administrador ativo — promova outro usuário a admin "
            "antes de rebaixar ou desativar este.",
        )

    # nome/email não são editáveis por esta tela (só papel/posto/ativo) — mantém os
    # valores atuais para não apagá-los na mesma UPDATE que a camada de dados exige.
    banco.usuarios_atualizar(id_, {
        "nome": atual["nome"], "email": atual["email"],
        "papel": papel, "empresa_id": int(empresa_id) if empresa_id else None,
        "ativo": ativo,
    })
    if not ativo:
        # Corta o acesso AGORA, não só em novos logins — sem isto uma sessão já aberta
        # continuava válida por até 1h depois de "desativado" no painel.
        auth_mod.remover_sessoes_usuario(id_)
    return {"atualizado": True}


@router.post("/api/usuarios/{id_}/senha")
def redefinir_senha(id_: int, payload: dict):
    senha = payload.get("senha") or ""
    if len(senha) < 8:
        raise HTTPException(400, "senha deve ter pelo menos 8 caracteres")
    if not banco.usuarios_definir_senha(id_, auth_mod.hash_senha(senha)):
        raise HTTPException(404, "Usuário não encontrado")
    # Redefinir senha é tipicamente resposta a "acho que vazou" — de nada adianta
    # trocar a senha e deixar a sessão antiga (cookie já vazado) continuar valendo.
    auth_mod.remover_sessoes_usuario(id_)
    return {"redefinida": True}
