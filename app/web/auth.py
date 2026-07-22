"""Rotas de autenticação: /criar-admin  /login  /logout"""
from __future__ import annotations
import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core import banco
from app.seguranca import sessao as auth_mod

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


# ── Criar administrador (primeiro acesso — sem usuários no banco) ─────────────

@router.get("/criar-admin")
def criar_admin_get(request: Request):
    if banco.contar_usuarios() > 0:
        return RedirectResponse("/login", status_code=303)
    flash: dict = {}
    raw = request.cookies.get("_flash_admin")
    if raw:
        try:
            flash = json.loads(raw)
        except Exception:
            pass
    resp = templates.TemplateResponse(request, "criar_admin.html", {
        "erro":  flash.get("erro"),
        "nome":  flash.get("nome", ""),
        "email": flash.get("email", ""),
    })
    resp.delete_cookie("_flash_admin")
    return resp


@router.post("/criar-admin")
async def criar_admin_post(
    nome:      str = Form(...),
    email:     str = Form(...),
    senha:     str = Form(...),
    confirmar: str = Form(...),
):
    if banco.contar_usuarios() > 0:
        return RedirectResponse("/login", status_code=303)

    nome_v  = nome.strip()
    email_v = email.strip().lower()
    erro = None

    if not nome_v or not email_v:
        erro = "Nome e e-mail são obrigatórios."
    elif senha != confirmar:
        erro = "As senhas não conferem."
    elif len(senha) < 8:
        erro = "A senha deve ter pelo menos 8 caracteres."

    if erro:
        resp = RedirectResponse("/criar-admin", status_code=303)
        resp.set_cookie("_flash_admin",
                        json.dumps({"erro": erro, "nome": nome_v, "email": email_v}),
                        httponly=True, samesite="lax", max_age=60)
        return resp

    uid = banco.criar_usuario(nome_v, email_v, auth_mod.hash_senha(senha), papel="admin")
    if uid is None:
        resp = RedirectResponse("/criar-admin", status_code=303)
        resp.set_cookie("_flash_admin",
                        json.dumps({"erro": "Erro ao criar usuário. Tente novamente.", "nome": nome_v, "email": email_v}),
                        httponly=True, samesite="lax", max_age=60)
        return resp

    token = auth_mod.criar_sessao(uid)
    # Cai em /postos: é onde o trabalho começa (implantação e diagnóstico por cliente).
    # "Ao Vivo" só é útil com o pipeline contínuo, que o servidor central não usa.
    resp = RedirectResponse("/postos", status_code=303)
    resp.set_cookie("sessao", token, httponly=True, samesite="lax", max_age=86400 * 7)
    return resp


# ── Login ─────────────────────────────────────────────────────────────────────

@router.get("/login")
def login_get(request: Request):
    if banco.contar_usuarios() == 0:
        return RedirectResponse("/criar-admin", status_code=303)
    flash: dict = {}
    raw = request.cookies.get("_flash_login")
    if raw:
        try:
            flash = json.loads(raw)
        except Exception:
            pass
    resp = templates.TemplateResponse(request, "login.html", {
        "erro":    None,
        "sucesso": flash.get("sucesso"),
        "email":   flash.get("email", ""),
    })
    resp.delete_cookie("_flash_login")
    return resp


@router.post("/login")
async def login_post(request: Request, email: str = Form(...), senha: str = Form(...)):
    if banco.contar_usuarios() == 0:
        return RedirectResponse("/criar-admin", status_code=303)

    user = banco.buscar_usuario_email(email.strip().lower())
    if not user or not auth_mod.verificar_senha(senha, user["senha"]):
        return templates.TemplateResponse(request, "login.html", {
            "erro":    "E-mail ou senha incorretos.",
            "sucesso": None,
            "email":   email.strip().lower(),
        })

    token = auth_mod.criar_sessao(user["id"])
    resp = RedirectResponse("/postos", status_code=303)
    resp.set_cookie("sessao", token, httponly=True, samesite="lax", max_age=86400 * 7)
    return resp


# ── Logout ────────────────────────────────────────────────────────────────────

@router.get("/logout")
def logout(request: Request):
    token = request.cookies.get("sessao")
    if token:
        auth_mod.remover_sessao(token)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("sessao")
    return resp
