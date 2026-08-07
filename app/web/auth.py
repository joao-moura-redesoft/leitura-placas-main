"""Rotas de autenticação: /criar-admin  /login  /logout"""
from __future__ import annotations
import json
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core import banco
from app.seguranca import sessao as auth_mod
from app.seguranca import tentativas as freio

log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _no_store(resp):
    """Evita que o navegador guarde página/redirect de login em cache entre restarts."""
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── Criar administrador (primeiro acesso — sem usuários no banco) ─────────────

@router.get("/criar-admin")
def criar_admin_get(request: Request):
    if banco.contar_usuarios() > 0:
        return _no_store(RedirectResponse("/login", status_code=303))
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
    return _no_store(resp)


@router.post("/criar-admin")
async def criar_admin_post(
    nome:      str = Form(...),
    email:     str = Form(...),
    senha:     str = Form(...),
    confirmar: str = Form(...),
):
    if banco.contar_usuarios() > 0:
        return _no_store(RedirectResponse("/login", status_code=303))

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
        return _no_store(resp)

    uid = banco.criar_usuario(nome_v, email_v, auth_mod.hash_senha(senha), papel="admin")
    if uid is None:
        resp = RedirectResponse("/criar-admin", status_code=303)
        resp.set_cookie("_flash_admin",
                        json.dumps({"erro": "Erro ao criar usuário. Tente novamente.", "nome": nome_v, "email": email_v}),
                        httponly=True, samesite="lax", max_age=60)
        return _no_store(resp)

    token = auth_mod.criar_sessao(uid)
    # Cai em /postos: é onde o trabalho começa (implantação e diagnóstico por cliente).
    # "Ao Vivo" só é útil com o pipeline contínuo, que o servidor central não usa.
    resp = RedirectResponse("/postos", status_code=303)
    resp.set_cookie("sessao", token, httponly=True, samesite="lax", max_age=86400 * 7)
    return _no_store(resp)


# ── Login ─────────────────────────────────────────────────────────────────────

@router.get("/login")
def login_get(request: Request):
    if banco.contar_usuarios() == 0:
        return _no_store(RedirectResponse("/criar-admin", status_code=303))
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
    return _no_store(resp)


@router.post("/login")
async def login_post(request: Request, email: str = Form(...), senha: str = Form(...)):
    if banco.contar_usuarios() == 0:
        return _no_store(RedirectResponse("/criar-admin", status_code=303))

    email_v = email.strip().lower()
    ip = _ip(request)

    def _falha(msg: str):
        return _no_store(templates.TemplateResponse(request, "login.html", {
            "erro": msg, "sucesso": None, "email": email_v,
        }))

    # Força bruta: depois de algumas falhas o par e-mail/IP fica de castigo por um tempo
    # que dobra a cada nova falha. Checado ANTES do bcrypt — verificar a senha aqui só
    # gastaria CPU para dar a mesma resposta.
    espera = freio.segundos_de_bloqueio(email_v, ip)
    if espera > 0:
        log.warning("Login bloqueado por excesso de tentativas: email=%s ip=%s (faltam %ds)",
                    email_v, ip, espera)
        return _falha(f"Muitas tentativas. Aguarde {espera} segundo(s) e tente de novo.")

    user = banco.buscar_usuario_email(email_v)
    if not user or not auth_mod.verificar_senha(senha, user["senha"]):
        freio.registrar_falha(email_v, ip)
        log.info("Login falhou: email=%s ip=%s", email_v, ip)
        return _falha("E-mail ou senha incorretos.")

    # Conta desativada no painel de usuários. Sem esta trava o campo `ativo` não fazia
    # nada: desativar alguém deixava o login dele funcionando normalmente.
    if not user["ativo"]:
        freio.registrar_falha(email_v, ip)
        return _falha("Esta conta está desativada. Procure um administrador.")

    freio.registrar_sucesso(email_v, ip)
    token = auth_mod.criar_sessao(user["id"])
    resp = RedirectResponse("/postos", status_code=303)
    resp.set_cookie("sessao", token, httponly=True, samesite="lax", max_age=86400 * 7)
    return _no_store(resp)


# ── Logout ────────────────────────────────────────────────────────────────────

@router.get("/logout")
def logout(request: Request):
    token = request.cookies.get("sessao")
    if token:
        auth_mod.remover_sessao(token)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("sessao")
    return _no_store(resp)
