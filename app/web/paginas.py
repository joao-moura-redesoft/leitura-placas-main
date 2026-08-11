"""Páginas HTML — Jinja2."""
from __future__ import annotations
from app.core import banco
from app.core import config
from app.web import deps
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


def _ctx(request: Request, **extra) -> dict:
    """Contexto comum a toda página logada: `usuario` (None fora de sessão — acesso só
    por api_key global, ou rota pública) alimenta o `{% if %}` do menu em base.html."""
    return {"usuario": deps.usuario_atual(request), **extra}


def _pagina_admin(request: Request):
    """Páginas estruturais (cadastro avulso, configuração do sistema, ferramentas
    internas) — usuário 'cliente' é mandado de volta para `/postos`, não vê nem o 403
    cru. Retorna a resposta de redirecionamento, ou None se pode seguir."""
    if not deps.eh_admin(request):
        return RedirectResponse("/postos", status_code=303)
    return None


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
    if (r := _pagina_admin(request)) is not None:
        return r
    return templates.TemplateResponse(request, "setup.html", _ctx(request))


@router.get("/historico")
def historico(request: Request):
    return templates.TemplateResponse(request, "historico.html", _ctx(request))


@router.get("/listas")
def listas(request: Request):
    return templates.TemplateResponse(request, "listas.html", _ctx(request))


@router.get("/dashboard")
def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", _ctx(request))


@router.get("/cameras")
def cameras(request: Request):
    if (r := _pagina_admin(request)) is not None:
        return r
    return templates.TemplateResponse(request, "cameras.html", _ctx(request))


@router.get("/postos")
def postos(request: Request):
    return templates.TemplateResponse(request, "postos.html", _ctx(request))


@router.get("/posto/novo")
def posto_novo(request: Request):
    if (r := _pagina_admin(request)) is not None:
        return r
    return templates.TemplateResponse(request, "posto_novo.html", _ctx(request))


@router.get("/posto/{empresa_id}")
def posto(request: Request, empresa_id: int):
    # Cliente só entra no próprio posto — mandado de volta para a lista (que já só
    # mostra o dele) em vez de um 404 seco.
    escopo = deps.empresa_do_usuario(request)
    if escopo is not None and empresa_id != escopo:
        return RedirectResponse("/postos", status_code=303)
    return templates.TemplateResponse(request, "posto.html", _ctx(request, empresa_id=empresa_id))


@router.get("/entidades")
def entidades(request: Request):
    if (r := _pagina_admin(request)) is not None:
        return r
    return templates.TemplateResponse(request, "entidades.html", _ctx(request))


@router.get("/empresas")
def empresas(request: Request):
    if (r := _pagina_admin(request)) is not None:
        return r
    return templates.TemplateResponse(request, "empresas.html", _ctx(request))


@router.get("/automacoes")
def automacoes(request: Request):
    if (r := _pagina_admin(request)) is not None:
        return r
    return templates.TemplateResponse(request, "automacoes.html", _ctx(request))


@router.get("/bicos")
def bicos(request: Request):
    if (r := _pagina_admin(request)) is not None:
        return r
    return templates.TemplateResponse(request, "bicos.html", _ctx(request))


@router.get("/roi-camera/{camera_id}")
def roi_camera(request: Request, camera_id: int, bico: int | None = None):
    """Editor de áreas por CÂMERA: uma imagem, um retângulo por bico.

    É como o trabalho acontece de verdade ("olho a imagem e marco onde fica cada bico"),
    e evita uma captura RTSP por bico — a câmera só aceita uma conexão por vez.

    Admin-only: é ajuste fino de captura (qualidade da leitura), não visão de dados —
    fica com a equipe RedSoft, igual ao resto do cadastro estrutural.
    """
    if (r := _pagina_admin(request)) is not None:
        return r
    return templates.TemplateResponse(
        request, "roi_camera.html", _ctx(request, camera_id=camera_id, bico_id=bico))


@router.get("/roi-bico/{bico_id}")
def roi_bico(bico_id: int):
    """Compatibilidade: links antigos por bico caem no editor da câmera dele."""
    b = banco.bicos_obter(bico_id)
    if not b:
        return RedirectResponse("/postos", status_code=303)
    return RedirectResponse(f"/roi-camera/{b['camera_id']}?bico={bico_id}", status_code=303)


@router.get("/configuracao")
def configuracao(request: Request):
    if (r := _pagina_admin(request)) is not None:
        return r
    return templates.TemplateResponse(request, "configuracao.html", _ctx(request))


@router.get("/testes")
def testes(request: Request):
    if (r := _pagina_admin(request)) is not None:
        return r
    return templates.TemplateResponse(request, "testes.html", _ctx(request))


@router.get("/auditoria")
def auditoria(request: Request):
    if (r := _pagina_admin(request)) is not None:
        return r
    return templates.TemplateResponse(request, "auditoria.html", _ctx(request))


@router.get("/documentacao")
def documentacao(request: Request):
    return templates.TemplateResponse(request, "documentacao.html", _ctx(request))
