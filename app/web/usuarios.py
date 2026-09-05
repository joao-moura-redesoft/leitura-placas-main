"""Gestão de usuários do painel: papéis 'admin' (tudo) × 'operador' (opera, não
administra, não preso a posto) × 'cliente' (preso a UM posto).

Diferente da versão anterior, este router NÃO tem gate de admin no `include_router`
(app/servidor.py) — cada rota decide sozinha. Existem rotas de verdade acessíveis a
QUALQUER usuário logado (saber quem é, trocar a própria senha, gerir as próprias
sessões); o resto (listar, criar, editar outros usuários) continua atrás de
`Depends(deps.exigir_admin)`.
"""
from __future__ import annotations
import secrets
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core import banco
from app.core import config
from app.seguranca import email as email_mod
from app.seguranca import sessao as auth_mod
from app.web import deps

router = APIRouter()


templates = Jinja2Templates(directory="app/web/templates")

_PAPEIS_VALIDOS = ("admin", "operador", "cliente")


def _sem_senha(user: dict) -> dict:
    return {k: v for k, v in user.items() if k != "senha"}


# ── Acessível a QUALQUER usuário logado ──────────────────────────────────────────

@router.get("/usuarios")
def pagina(request: Request):
    if not deps.eh_admin(request):
        return RedirectResponse("/postos", status_code=303)
    return templates.TemplateResponse(request, "usuarios.html", {"usuario": deps.usuario_atual(request)})


@router.get("/minha-conta")
def pagina_minha_conta(request: Request):
    return templates.TemplateResponse(request, "minha_conta.html", {"usuario": deps.usuario_atual(request)})


@router.get("/api/usuarios/eu")
def eu(request: Request):
    """Quem está logado — usado pela UI pra saber o próprio nome/papel, e pela troca
    de senha self-service abaixo. 404 na leitura via api_key: não é uma sessão de
    usuário, não tem "eu"."""
    usuario = deps.usuario_atual(request)
    if usuario is None:
        raise HTTPException(404, "Sem sessão de usuário (acesso via api_key)")
    return _sem_senha(usuario)


@router.post("/api/usuarios/eu/senha")
def trocar_a_propria_senha(payload: dict, request: Request):
    """Self-service: qualquer usuário logado troca a PRÓPRIA senha, sem precisar de
    admin. Exige a senha atual (diferente do reset feito por um admin em
    `/api/usuarios/{id}/senha`) — é o que distingue "eu decidi trocar" de "alguém com
    a sessão aberta decidiu trocar por mim"."""
    usuario = deps.usuario_atual(request)
    if usuario is None:
        raise HTTPException(404, "Sem sessão de usuário (acesso via api_key)")

    senha_atual = payload.get("senha_atual") or ""
    senha_nova = payload.get("senha_nova") or ""
    if not auth_mod.verificar_senha(senha_atual, usuario["senha"]):
        raise HTTPException(400, "Senha atual incorreta.")
    erro = auth_mod.senha_fraca(senha_nova)
    if erro:
        raise HTTPException(400, erro)

    banco.usuarios_definir_senha(usuario["id"], auth_mod.hash_senha(senha_nova))
    banco.auditoria_registrar(usuario_id=usuario["id"], usuario_nome=usuario["nome"],
                              acao="senha_trocada_self", alvo_tipo="usuario", alvo_id=usuario["id"])
    # Derruba as OUTRAS sessões (ex.: cookie vazado em outro navegador) — preserva a
    # que está fazendo a troca agora, senão a própria pessoa seria expulsa da tela.
    token_atual = request.cookies.get("sessao")
    auth_mod.remover_sessoes_usuario(usuario["id"], exceto_token=token_atual)
    return {"trocada": True}


@router.get("/api/usuarios/eu/sessoes")
def minhas_sessoes(request: Request):
    """"Meus dispositivos" — todas as sessões ativas da própria conta, pra descobrir
    (e revogar) um acesso esquecido aberto em outro lugar."""
    usuario = deps.usuario_atual(request)
    if usuario is None:
        raise HTTPException(404, "Sem sessão de usuário (acesso via api_key)")
    token_atual = request.cookies.get("sessao")
    return [
        {**s, "atual": s["token"] == token_atual}
        for s in banco.sessoes_listar_do_usuario(usuario["id"])
    ]


@router.delete("/api/usuarios/eu/sessoes/{token}")
def revogar_minha_sessao(token: str, request: Request):
    usuario = deps.usuario_atual(request)
    if usuario is None:
        raise HTTPException(404, "Sem sessão de usuário (acesso via api_key)")
    sessao = banco.sessao_resolver(token)
    # `sessao["id"]` é o id do DONO da sessão (ver sessao_resolver) — só revoga a
    # própria, mesmo que alguém tente adivinhar o token de outra conta.
    if sessao is None or sessao["id"] != usuario["id"]:
        raise HTTPException(404, "Sessão não encontrada")
    banco.sessao_remover(token)
    return {"revogada": True}


# ── Admin-only ────────────────────────────────────────────────────────────────────

@router.get("/api/usuarios", dependencies=[Depends(deps.exigir_admin)])
def listar():
    return banco.usuarios_listar()


@router.post("/api/usuarios", dependencies=[Depends(deps.exigir_admin)])
def criar(payload: dict, request: Request):
    nome = (payload.get("nome") or "").strip()
    email = (payload.get("email") or "").strip().lower()
    papel = payload.get("papel") or "cliente"
    empresa_id = payload.get("empresa_id")
    convidar = bool(payload.get("convidar"))
    senha = payload.get("senha") or ""

    if not nome or not email:
        raise HTTPException(400, "nome e email são obrigatórios")
    if papel not in _PAPEIS_VALIDOS:
        raise HTTPException(400, f"papel deve ser um de {_PAPEIS_VALIDOS}")
    if papel == "cliente":
        if not empresa_id:
            raise HTTPException(400, "empresa_id é obrigatório para papel 'cliente'")
        if not banco.empresas_obter(deps.inteiro_ou_400(empresa_id, 'empresa_id')):
            raise HTTPException(400, f"Empresa {empresa_id} não encontrada")

    cfg = config.carregar()
    if convidar:
        # Convite por e-mail: ninguém define a senha agora — um placeholder
        # aleatório e inutilizável entra no lugar (nem o admin que criou sabe qual é);
        # a pessoa convidada define a senha de verdade pelo link (mesmo mecanismo do
        # "esqueci minha senha").
        if not email_mod.configurado(cfg):
            raise HTTPException(400, "Convite por e-mail exige SMTP configurado em Configuração. "
                                     "defina uma senha diretamente, ou configure o envio antes.")
        senha_hash = auth_mod.hash_senha(secrets.token_urlsafe(24))
    else:
        erro = auth_mod.senha_fraca(senha)
        if erro:
            raise HTTPException(400, erro)
        senha_hash = auth_mod.hash_senha(senha)

    uid = banco.criar_usuario(
        nome, email, senha_hash, papel=papel,
        empresa_id=int(empresa_id) if empresa_id else None,
    )
    if uid is None:
        raise HTTPException(409, f"E-mail {email} já cadastrado")

    quem_id, quem_nome = deps.quem_pede(request)
    banco.auditoria_registrar(
        usuario_id=quem_id, usuario_nome=quem_nome, acao="usuario_criado",
        alvo_tipo="usuario", alvo_id=uid,
        detalhe=f"email={email} papel={papel}" + (" (convite por e-mail)" if convidar else ""),
    )

    if convidar:
        token = banco.reset_token_criar(uid)
        link = f"{email_mod.url_base(request, cfg)}/redefinir-senha/{token}"
        email_mod.enviar(
            email, "Você foi convidado | Leitura de Placas",
            f"Olá, {nome}.\n\n"
            f"Uma conta foi criada para você no sistema de leitura de placas (papel: {papel}).\n\n"
            f"Para definir sua senha e acessar, use o link abaixo — ele vale por 2 horas:\n{link}\n\n"
            f"Se você não esperava este e-mail, pode ignorá-lo.",
            cfg=cfg,
        )
    return {"id": uid}


def _como_bool(valor, padrao: bool) -> bool:
    """Interpreta o que veio no JSON como booleano, com o valor atual como default.

    `bool("false")` é True — e um formulário HTML manda exatamente essa string. Aceita as
    mesmas grafias que `config.get_bool` para o sistema inteiro concordar sobre o que é
    "não".
    """
    if valor is None:
        return bool(padrao)
    if isinstance(valor, str):
        return valor.strip().lower() in ("sim", "true", "1", "yes", "on")
    return bool(valor)


@router.put("/api/usuarios/{id_}", dependencies=[Depends(deps.exigir_admin)])
def atualizar(id_: int, payload: dict, request: Request):
    atual = banco.buscar_usuario_id(id_)
    if not atual:
        raise HTTPException(404, "Usuário não encontrado")

    nome = (payload.get("nome") or atual["nome"]).strip()
    email = (payload.get("email") or atual["email"]).strip().lower()
    # O default de CADA campo é o valor ATUAL, nunca uma constante. Antes era
    # `payload.get("papel") or "cliente"`: um PUT parcial — `{"nome": "X", "empresa_id": 3}`,
    # que a tela manda ao editar só o nome — REBAIXAVA um admin a cliente em silêncio.
    # Mesma coisa em `ativo`, que voltava a True e ressuscitava conta desativada.
    # (Auditoria 27/08/2026, achado M6.)
    papel = payload.get("papel") or atual["papel"]
    empresa_id = payload["empresa_id"] if "empresa_id" in payload else atual["empresa_id"]
    # `_como_bool` e não `bool(...)`: com `bool`, a string "false" vinda de um form vira
    # True — o oposto do que quem enviou pediu.
    ativo = _como_bool(payload.get("ativo"), atual["ativo"])
    senha = payload.get("senha") or ""

    if papel not in _PAPEIS_VALIDOS:
        raise HTTPException(400, f"papel deve ser um de {_PAPEIS_VALIDOS}")
    if papel == "cliente":
        if not empresa_id:
            raise HTTPException(400, "empresa_id é obrigatório para papel 'cliente'")
        if not banco.empresas_obter(deps.inteiro_ou_400(empresa_id, 'empresa_id')):
            raise HTTPException(400, f"Empresa {empresa_id} não encontrada")
    if senha:
        erro = auth_mod.senha_fraca(senha)
        if erro:
            raise HTTPException(400, erro)

    # Autoproteção: ninguém mexe no PRÓPRIO status administrativo por aqui — mesmo que
    # sobrem outros admins. É uma trava a mais além de "não pode ser o último admin"
    # (abaixo): sem ela, um admin distraído se rebaixa ou se desativa sozinho e precisa
    # de OUTRO admin pra desfazer, o que é evitável de graça.
    quem_pede = deps.usuario_atual(request)
    eh_auto_edicao = quem_pede is not None and quem_pede["id"] == id_
    if eh_auto_edicao and atual["papel"] == "admin" and (papel != "admin" or not ativo):
        raise HTTPException(400, "Você não pode alterar seu próprio papel ou status. Peça a outro administrador.")

    vira_nao_admin = atual["papel"] == "admin" and (papel != "admin" or not ativo)
    if vira_nao_admin and banco.usuarios_contar_admins_ativos(excluir_id=id_) == 0:
        raise HTTPException(
            400,
            "Este é o último administrador ativo. Promova outro usuário a admin "
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

    mudancas = []
    if nome != atual["nome"]:
        mudancas.append("nome")
    if email != atual["email"]:
        mudancas.append("email")
    if papel != atual["papel"]:
        mudancas.append(f"papel:{atual['papel']}->{papel}")
    if bool(atual["ativo"]) != ativo:
        mudancas.append(f"ativo:{bool(atual['ativo'])}->{ativo}")
    if senha:
        mudancas.append("senha")
    quem_id, quem_nome = deps.quem_pede(request)
    banco.auditoria_registrar(
        usuario_id=quem_id, usuario_nome=quem_nome, acao="usuario_atualizado",
        alvo_tipo="usuario", alvo_id=id_, detalhe=", ".join(mudancas) or "sem mudanças",
    )

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


@router.post("/api/usuarios/{id_}/senha", dependencies=[Depends(deps.exigir_admin)])
def redefinir_senha(id_: int, payload: dict, request: Request):
    """Admin redefine a senha de OUTRO usuário — sem pedir a senha atual dele (é
    autoridade administrativa, não self-service; ver `trocar_a_propria_senha` acima
    para o caso do próprio usuário)."""
    senha = payload.get("senha") or ""
    erro = auth_mod.senha_fraca(senha)
    if erro:
        raise HTTPException(400, erro)
    if not banco.usuarios_definir_senha(id_, auth_mod.hash_senha(senha)):
        raise HTTPException(404, "Usuário não encontrado")

    quem_id, quem_nome = deps.quem_pede(request)
    banco.auditoria_registrar(usuario_id=quem_id, usuario_nome=quem_nome,
                              acao="senha_redefinida_por_admin", alvo_tipo="usuario", alvo_id=id_)

    # Redefinir senha é tipicamente resposta a "acho que vazou" — de nada adianta
    # trocar a senha e deixar a sessão antiga (cookie já vazado) continuar valendo.
    # Exceção: a própria sessão de quem está redefinindo AGORA (admin resetando a si
    # mesmo via este endpoint, em vez do self-service acima).
    quem_pede = deps.usuario_atual(request)
    eh_auto_edicao = quem_pede is not None and quem_pede["id"] == id_
    token_atual = request.cookies.get("sessao") if eh_auto_edicao else None
    auth_mod.remover_sessoes_usuario(id_, exceto_token=token_atual)
    return {"redefinida": True}
