"""Páginas HTML — Jinja2."""
from __future__ import annotations
import json
from app.core import banco
from app.core import config
from app.visao import feira
from app.visao import feira_fichas
from app.web import deps
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


def _ctx(request: Request, **extra) -> dict:
    """Contexto comum a toda página logada: `usuario` (None fora de sessão — acesso só
    por api_key global, ou rota pública) alimenta o `{% if %}` do menu em base.html.

    `feira_on` decide se o link da vitrine aparece na navbar — só quando o modo feira
    está de fato armado (`feira.ativo`), senão a aba levaria a um redirect."""
    return {"usuario": deps.usuario_atual(request),
            "feira_on": feira.ativo(config.carregar()), **extra}


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


@router.get("/feira")
def feira_vitrine(request: Request):
    """Kiosk de demonstração para a feira — câmera ao vivo + card "Bem-vindo!".

    Só existe com o modo feira ARMADO (`feira.ativo`): sem posto de demonstração e sem
    placa cadastrada não há o que exibir, então cai em /postos (e o link nem aparece na
    navbar — ver `_ctx`). Roda em tela cheia, fora do layout com navbar, para o estande
    ter uma vitrine e não a tela de operação.
    """
    cfg = config.carregar()
    if not feira.ativo(cfg):
        return RedirectResponse("/postos", status_code=303)

    # Resolve o bico/câmera do posto de demonstração — é ele que o loop hands-free lê e de
    # onde vem o MJPEG. A árvore é criada junta por POST /api/feira/posto, então o primeiro
    # bico com câmera é o da demonstração.
    empresa_id = feira.empresa_demo(cfg)
    bico = None
    for auto in banco.automacoes_listar(empresa_id=empresa_id):
        bicos = banco.bicos_listar(automacao_id=auto["id"])
        if bicos:
            bico = bicos[0]
            break

    return templates.TemplateResponse(request, "feira.html", _ctx(
        request,
        camera_id=(bico or {}).get("camera_id"),
        bico_id=(bico or {}).get("id"),
        fichas_json=json.dumps(feira_fichas.carregar_fichas(), ensure_ascii=False),
    ))


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
def bicos(request: Request, automacao_id: int | None = None):
    """A tela de bicos avulsa foi aposentada — mesmo caminho da antiga "Ao Vivo" global.

    Ela cadastrava o mesmo bico que o modal de /posto/{id}, mas com regras próprias: não
    barrava escolher a mesma câmera nos dois slots e não avisava que trocar a segunda
    câmera apaga a área já desenhada nela (a área está em coordenadas do frame antigo).
    Duas portas para o mesmo cadastro divergem sempre, e aqui a divergência custava um
    desenho perdido em silêncio.

    A tela do posto é a que tem o contexto que a decisão exige: as câmeras daquele posto,
    a imagem ao vivo de cada uma e o checklist do que falta para operar.
    """
    # O destino contextual sai só para admin. `automacao_id` é um inteiro sequencial: para
    # um usuário 'cliente' o redirecionamento revelaria a que posto cada automação pertence
    # (bastava iterar e ler o Location), e a página anterior era admin-only justamente por
    # isso. Cliente vai para /postos, que já mostra apenas o posto dele.
    if automacao_id is not None and deps.eh_admin(request):
        automacao = banco.automacoes_obter(automacao_id)
        if automacao:
            return RedirectResponse(f"/posto/{automacao['empresa_id']}", status_code=303)
    return RedirectResponse("/postos", status_code=303)


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
def roi_bico(request: Request, bico_id: int):
    """Compatibilidade: links antigos por bico caem no editor da câmera dele.

    Admin-only como o destino (`/roi-camera` acima): sem o gate, este redirecionamento
    resolvia bico_id → camera_id para qualquer usuário logado, e bico_id é um inteiro
    sequencial pequeno — bastava iterar e ler o `Location` para mapear a relação
    bico/câmera de todos os postos. Mesma classe do vazamento fechado no preview de bico
    (ver a mudança em app/web/api.py que tirou o JPEG de dentro de static/). O acesso em si
    já estava barrado no destino; o que escapava era a relação.
    """
    if (r := _pagina_admin(request)) is not None:
        return r
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
