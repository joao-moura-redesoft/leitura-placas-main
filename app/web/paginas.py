"""Páginas HTML — Jinja2."""
from __future__ import annotations
from app.core import banco
from app.core import config
from app.web.usuarios import usuario_atual
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


@router.get("/")
def index():
    """A antiga tela "Ao Vivo" global foi aposentada.

    Ela listava as câmeras de TODOS os clientes num grid só e o botão de leitura dela
    analisava o quadro inteiro, ignorando a área do bico — podia devolver a placa da
    vaga vizinha. O equivalente correto vive em /posto/{id}, escopado por posto e
    lendo por bico.
    """
    cfg = config.carregar()
    if cfg.get("implantado", "nao").lower() != "sim":
        return RedirectResponse("/setup", status_code=302)
    return RedirectResponse("/postos", status_code=303)


@router.get("/setup")
def setup(request: Request):
    return templates.TemplateResponse(request, "setup.html")


@router.get("/historico")
def historico(request: Request):
    return templates.TemplateResponse(request, "historico.html")


@router.get("/listas")
def listas(request: Request):
    return templates.TemplateResponse(request, "listas.html")


@router.get("/dashboard")
def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html")


@router.get("/cameras")
def cameras(request: Request):
    return templates.TemplateResponse(request, "cameras.html")


@router.get("/postos")
def postos(request: Request):
    return templates.TemplateResponse(request, "postos.html")


@router.get("/posto/novo")
def posto_novo(request: Request):
    return templates.TemplateResponse(request, "posto_novo.html")


@router.get("/posto/{empresa_id}")
def posto(request: Request, empresa_id: int):
    return templates.TemplateResponse(request, "posto.html", {"empresa_id": empresa_id})


@router.get("/entidades")
def entidades(request: Request):
    return templates.TemplateResponse(request, "entidades.html")


@router.get("/empresas")
def empresas(request: Request):
    return templates.TemplateResponse(request, "empresas.html")


@router.get("/automacoes")
def automacoes(request: Request):
    return templates.TemplateResponse(request, "automacoes.html")


@router.get("/bicos")
def bicos(request: Request):
    return templates.TemplateResponse(request, "bicos.html")


@router.get("/roi-camera/{camera_id}")
def roi_camera(request: Request, camera_id: int, bico: int | None = None):
    """Editor de áreas por CÂMERA: uma imagem, um retângulo por bico.

    É como o trabalho acontece de verdade ("olho a imagem e marco onde fica cada bico"),
    e evita uma captura RTSP por bico — a câmera só aceita uma conexão por vez.
    """
    return templates.TemplateResponse(
        request, "roi_camera.html", {"camera_id": camera_id, "bico_id": bico})


@router.get("/roi-bico/{bico_id}")
def roi_bico(bico_id: int):
    """Compatibilidade: links antigos por bico caem no editor da câmera dele."""
    b = banco.bicos_obter(bico_id)
    if not b:
        return RedirectResponse("/postos", status_code=303)
    return RedirectResponse(f"/roi-camera/{b['camera_id']}?bico={bico_id}", status_code=303)


@router.get("/usuarios")
def usuarios(request: Request):
    """Somente admin — a API (/api/usuarios) já bloqueia com 403, isto aqui evita
    renderizar o painel inteiro pra quem não pode usá-lo."""
    user = usuario_atual(request)
    if not user or user.get("papel") != "admin":
        return RedirectResponse("/postos", status_code=303)
    return templates.TemplateResponse(request, "usuarios.html")


@router.get("/configuracao")
def configuracao(request: Request):
    return templates.TemplateResponse(request, "configuracao.html")


@router.get("/testes")
def testes(request: Request):
    return templates.TemplateResponse(request, "testes.html")


@router.get("/documentacao")
def documentacao(request: Request):
    return templates.TemplateResponse(request, "documentacao.html")
