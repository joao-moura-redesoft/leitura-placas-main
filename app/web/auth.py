"""Rotas de autenticação: /criar-admin  /login  /logout  /esqueci-senha  /redefinir-senha"""
from __future__ import annotations
import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core import banco
from app.core import config
from app.seguranca import email as email_mod
from app.seguranca import sessao as auth_mod
from app.seguranca import tentativas

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


def _cookie_sessao(resp, token: str, cfg: dict, request=None) -> None:
    """Marca o cookie de sessão como `Secure` sempre que a conexão for HTTPS.

    Antes valia só `cookie_secure=sim`, que é `nao` por padrão — e o default existe por um
    bom motivo (ligar sempre quebraria http://localhost:14000 no primeiro boot). O efeito
    colateral era que uma instalação atrás de TLS, mas sem ninguém ter mexido no config,
    servia o cookie de sessão SEM a flag. (Auditoria 27/08/2026.)

    Detectar por requisição resolve os dois casos sem escolha manual: em HTTP local o
    cookie sai sem `Secure` e o acesso continua funcionando; em HTTPS ele sai com. O
    config vira um FORÇAR, para quem está atrás de proxy que termina TLS e encaminha em
    HTTP puro sem `X-Forwarded-Proto`.
    """
    seguro = config.get_bool(cfg, "cookie_secure")
    if not seguro and request is not None:
        proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        seguro = proto == "https" or request.url.scheme == "https"
    resp.set_cookie(
        "sessao", token, httponly=True, samesite="lax", max_age=86400 * 7,
        secure=seguro,
    )


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
    request:   Request,
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
    else:
        erro = auth_mod.senha_fraca(senha)

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

    cfg = config.carregar()
    banco.usuarios_marcar_login(uid)
    banco.auditoria_registrar(usuario_id=uid, usuario_nome=nome_v, acao="bootstrap_admin",
                              alvo_tipo="usuario", alvo_id=uid, detalhe=f"email={email_v}")
    token = auth_mod.criar_sessao(uid)
    # Cai em /postos: é onde o trabalho começa (implantação e diagnóstico por cliente).
    # "Ao Vivo" só é útil com o pipeline contínuo, que o servidor central não usa.
    resp = RedirectResponse("/postos", status_code=303)
    _cookie_sessao(resp, token, cfg, request)
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

    email_norm = email.strip().lower()
    ip = request.client.host if request.client else "?"

    # Freio de força bruta contado por E-MAIL e por IP juntos (ver docstring de
    # tentativas.py): só por e-mail deixaria varrer vários e-mails de um IP só; só por
    # IP deixaria uma botnet atacar a mesma conta de vários lugares. Checado ANTES de
    # olhar a senha — inclusive uma senha CORRETA fica bloqueada enquanto durar a espera.
    espera = tentativas.segundos_de_bloqueio(email_norm, ip)
    if espera > 0:
        return templates.TemplateResponse(request, "login.html", {
            "erro":    f"Muitas tentativas — aguarde {espera}s e tente de novo.",
            "sucesso": None,
            "email":   email_norm,
        })

    user = banco.buscar_usuario_email(email_norm)
    # Verifica a senha SEMPRE, mesmo sem usuário ou com conta desativada. O curto-circuito
    # anterior (`not user or ... or verificar_senha(...)`) nunca chegava ao bcrypt nesses
    # dois casos, e com `_BCRYPT_ROUNDS = 12` isso é ~5 ms contra ~200 ms: uma diferença de
    # 40× visível a olho nu no DevTools, que dizia ao atacante quais e-mails existem e quais
    # contas estão ativas. O texto da resposta já era idêntico; o oráculo estava no relógio.
    # (Auditoria 27/08/2026, achado M7.)
    senha_ok = auth_mod.verificar_senha(senha, user["senha"] if user else auth_mod.HASH_DUMMY)
    # `not user["ativo"]`: antes um usuário desativado (banco.usuarios_atualizar) ainda
    # conseguia logar normalmente — nada checava esse campo no fluxo de login.
    if not user or not user["ativo"] or not senha_ok:
        tentativas.registrar_falha(email_norm, ip)
        banco.auditoria_registrar(
            usuario_id=None, usuario_nome="", acao="login_falha",
            alvo_tipo="usuario", alvo_id=user["id"] if user else "",
            detalhe=f"email={email_norm} ip={ip}",
        )
        return templates.TemplateResponse(request, "login.html", {
            "erro":    "E-mail ou senha incorretos.",
            "sucesso": None,
            "email":   email_norm,
        })

    tentativas.registrar_sucesso(email_norm, ip)
    banco.usuarios_marcar_login(user["id"])
    banco.auditoria_registrar(usuario_id=user["id"], usuario_nome=user["nome"], acao="login",
                              alvo_tipo="usuario", alvo_id=user["id"], detalhe=f"ip={ip}")
    token = auth_mod.criar_sessao(user["id"])
    resp = RedirectResponse("/postos", status_code=303)
    _cookie_sessao(resp, token, config.carregar(), request)
    return resp


# ── Esqueci minha senha ───────────────────────────────────────────────────────
# Chave por "reset:{email}" (não "{email}" puro) pra não compartilhar o contador de
# força bruta do LOGIN — pedir redefinição várias vezes não deveria bloquear a conta
# de tentar logar com a senha de sempre. O IP continua no mesmo balde de sempre: um IP
# martelando os dois endpoints é o mesmo tipo de abuso nos dois casos.

@router.get("/esqueci-senha")
def esqueci_senha_get(request: Request):
    cfg = config.carregar()
    return templates.TemplateResponse(request, "esqueci_senha.html", {
        "email_configurado": email_mod.configurado(cfg), "enviado": False, "email": "",
    })


@router.post("/esqueci-senha")
async def esqueci_senha_post(request: Request, email: str = Form(...)):
    cfg = config.carregar()
    email_norm = email.strip().lower()
    ip = request.client.host if request.client else "?"

    if not email_mod.configurado(cfg):
        # Sem SMTP configurado o recurso simplesmente não existe — avisa em vez de
        # fingir que enviou algo.
        return templates.TemplateResponse(request, "esqueci_senha.html", {
            "email_configurado": False, "enviado": False, "email": email_norm,
        })

    espera = tentativas.segundos_de_bloqueio(email_norm, ip, escopo="reset")
    if espera > 0:
        return templates.TemplateResponse(request, "esqueci_senha.html", {
            "email_configurado": True, "enviado": False, "email": email_norm,
            "erro": f"Muitos pedidos — aguarde {espera}s e tente de novo.",
        })
    # Escopo próprio: este balde não pode conversar com o do login (achado A6). O nome do
    # fluxo vai em `escopo`, e não grudado no e-mail, porque a chave de IP também precisa
    # dele — era exatamente ela que vazava entre os dois fluxos.
    tentativas.registrar_falha(email_norm, ip, escopo="reset")

    # Mesma resposta ("enviado") exista ou não a conta — não confirma e-mail
    # cadastrado (enumeration). Só envia de verdade quando a conta existe e está ativa.
    user = banco.buscar_usuario_email(email_norm)
    if user and user["ativo"]:
        token = banco.reset_token_criar(user["id"])
        link = f"{email_mod.url_base(request, cfg)}/redefinir-senha/{token}"
        email_mod.enviar(
            email_norm, "Redefinição de senha — Leitura de Placas",
            f"Olá, {user['nome']}.\n\n"
            f"Alguém (esperamos que você) pediu para redefinir a senha desta conta.\n\n"
            f"Para continuar, acesse o link abaixo — ele vale por 2 horas:\n{link}\n\n"
            f"Se você não pediu isso, é seguro ignorar este e-mail.",
            cfg=cfg,
        )
        banco.auditoria_registrar(
            usuario_id=None, usuario_nome="", acao="esqueci_senha_solicitado",
            alvo_tipo="usuario", alvo_id=user["id"], detalhe=f"email={email_norm} ip={ip}",
        )

    return templates.TemplateResponse(request, "esqueci_senha.html", {
        "email_configurado": True, "enviado": True, "email": email_norm,
    })


@router.get("/redefinir-senha/{token}")
def redefinir_senha_get(token: str, request: Request):
    valido = banco.reset_token_resolver(token) is not None
    return templates.TemplateResponse(request, "redefinir_senha.html", {
        "token": token, "valido": valido, "erro": None,
    })


@router.post("/redefinir-senha/{token}")
async def redefinir_senha_post(token: str, request: Request,
                                senha: str = Form(...), confirmar: str = Form(...)):
    dados_token = banco.reset_token_resolver(token)
    if dados_token is None:
        return templates.TemplateResponse(request, "redefinir_senha.html", {
            "token": token, "valido": False, "erro": None,
        })

    erro = "As senhas não conferem." if senha != confirmar else auth_mod.senha_fraca(senha)
    if erro:
        return templates.TemplateResponse(request, "redefinir_senha.html", {
            "token": token, "valido": True, "erro": erro,
        })

    # CONSOME o token (resolve + marca usado num único UPDATE) antes de mexer na senha.
    # Resolver e marcar em chamadas separadas deixava dois POSTs simultâneos com o mesmo
    # token passarem os dois. A validação lá em cima continua valendo para montar a tela;
    # esta é a que decide. (Auditoria 27/08/2026.)
    dados_token = banco.reset_token_consumir(token)
    if dados_token is None:
        return templates.TemplateResponse(request, "redefinir_senha.html", {
            "token": token, "valido": False, "erro": None,
        })

    user_id = dados_token["user_id"]
    user = banco.buscar_usuario_id(user_id)
    banco.usuarios_definir_senha(user_id, auth_mod.hash_senha(senha))
    # Sessões antigas não sobrevivem a uma troca de senha por link (o cenário típico
    # é "acho que perdi acesso" — uma sessão aberta em outro lugar não deveria continuar).
    auth_mod.remover_sessoes_usuario(user_id)
    banco.auditoria_registrar(
        usuario_id=None, usuario_nome="", acao="senha_redefinida_por_link",
        alvo_tipo="usuario", alvo_id=user_id,
        detalhe=f"email={user['email'] if user else '?'}",
    )

    cfg = config.carregar()
    novo_token = auth_mod.criar_sessao(user_id)
    resp = RedirectResponse("/postos", status_code=303)
    _cookie_sessao(resp, novo_token, cfg, request)
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
