"""Aplicação FastAPI — inicializa banco, pipeline e rotas."""
from __future__ import annotations
import asyncio
import logging
import platform
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Deve ficar aqui (e não só em main.py) porque em modo --reload o uvicorn
# importa "app.servidor:app" num subprocess separado, sem passar pelo main.py.
if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

import mimetypes

from app.core import banco
from app.core import broadcaster as bc
from app.core import config
from app.core import estado
from app.operacao import dns_server as dns_mod
from app.operacao import retencao as ret_mod
from app.operacao import supervisor as sv
from app.seguranca import sessao as auth_mod
from app.streaming import hls_encoder as hls_mod
from app.visao import pipeline
from app.web import api, paginas
from app.web import auth as auth_rotas
from app.web import cadastro as cadastro_rotas
from app.web import leitura as leitura_rotas
from app.web import stream as stream_rotas
from app.web import testes as testes_rotas
from app.web import usuarios as usuarios_rotas

# Garante MIME types corretos para servir arquivos HLS via StaticFiles
mimetypes.add_type("application/vnd.apple.mpegurl", ".m3u8")
mimetypes.add_type("video/mp2t", ".ts")


log = logging.getLogger(__name__)

# Rotas que não exigem autenticação via middleware
# /ws é público no middleware; a autenticação é feita dentro do endpoint.
# /api/healthz: liveness do container (só {"status":"ok"}, sem dado de cliente). O
# /api/health detalhado, com nome de câmera/posto, continua exigindo autenticação.
#
# /api/leitura NÃO está mais aqui: era totalmente público sob a justificativa de rodar
# na rede interna do posto, mas este servidor é central e multi-tenant — quem alcançasse
# a porta disparava leitura para qualquer CNPJ e recebia a placa de volta, além de poder
# prender a câmera por `leitura_timeout_seg` a cada chamada. Agora exige a api_key, que
# é exatamente o mecanismo pensado para integração sem browser (o sidecar Java do posto).
_PUBLICAS = frozenset({"/login", "/criar-admin", "/favicon.ico", "/ws", "/api/healthz"})

# ── Autorização por papel ────────────────────────────────────────────────────
# Só admin pode ALTERAR o sistema. A regra é por método, não por rota: qualquer
# POST/PUT/DELETE exige admin, salvo o que estiver em `_MUTACOES_OPERACIONAIS`.
# Feito assim de propósito — uma rota de escrita nova nasce protegida por padrão,
# em vez de depender de alguém lembrar de anotá-la.
_METODOS_LEITURA = frozenset({"GET", "HEAD", "OPTIONS"})

# Escritas que fazem parte da OPERAÇÃO do dia a dia, não da administração — quem só
# opera precisa delas. Prefixos, casados com `startswith`.
_MUTACOES_OPERACIONAIS = (
    "/api/bicos/",       # .../ler-placa-teste — disparar leitura é operação, não cadastro
)

# Páginas de administração: quem não é admin nem chega a renderizá-las (a API por trás
# já recusaria, mas abrir uma tela inteira que só devolve erro é pior que não ter o link).
_PAGINAS_ADMIN = frozenset({
    "/configuracao", "/usuarios", "/entidades", "/empresas", "/automacoes",
    "/bicos", "/cameras", "/testes", "/posto/novo", "/setup",
})


def _pode_escrever(path: str) -> bool:
    return any(path.startswith(p) for p in _MUTACOES_OPERACIONAIS)


class _AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Arquivos estáticos e streams sempre públicos
        if (path.startswith("/static/") or path.startswith("/testes/fotos/")
                or path.startswith("/testes/resultados/")
                or path in _PUBLICAS):
            return await call_next(request)

        # Autenticação via cookie de sessão. Vem ANTES do check de bootstrap porque é o
        # caminho comum (toda request de quem já está logado) — assim o caso normal paga
        # uma consulta ao banco, não duas.
        token = request.cookies.get("sessao")
        usuario = auth_mod.usuario_autenticado(token)
        # Publicado em `request.state` (compartilhado via scope) para rotas e templates
        # saberem QUEM está logado sem repetir a consulta que o middleware acabou de fazer.
        request.state.usuario = usuario
        if usuario is not None:
            negado = self._negar_por_papel(request, path, usuario)
            if negado is not None:
                return negado
            return await call_next(request)

        # Sem usuários → redireciona para criar o primeiro admin
        if banco.contar_usuarios() == 0:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Servidor não configurado. Acesse /criar-admin."}, status_code=503)
            return RedirectResponse("/criar-admin", status_code=303)

        # Autenticação via api_key (para integrações externas sem browser). A chave é
        # configurada por um admin e vale como acesso de admin — é o canal do roteador
        # do posto, que não tem sessão nem papel.
        cfg = config.carregar()
        api_key = cfg.get("api_key", "").strip()
        if api_key:
            h = request.headers.get("X-API-Key", "")
            q = request.query_params.get("api_key", "")
            if h == api_key or q == api_key:
                resp = await call_next(request)
                if token:
                    resp.delete_cookie("sessao")
                return resp

        # Não autenticado — limpa cookie de sessão morto (expirado ou de antes de um
        # restart) pra não ficar sendo reenviado por dias sem nunca mais validar.
        if path.startswith("/api/") or path.startswith("/stream/"):
            resp = JSONResponse({"detail": "Não autenticado."}, status_code=401)
        else:
            resp = RedirectResponse("/login", status_code=303)
        if token:
            resp.delete_cookie("sessao")
        return resp

    @staticmethod
    def _negar_por_papel(request: Request, path: str, usuario: dict):
        """Recusa a request quando o papel não alcança. Devolve a resposta de recusa,
        ou None se estiver liberada."""
        if usuario.get("papel") == "admin":
            return None
        if path in _PAGINAS_ADMIN:
            return RedirectResponse("/postos", status_code=303)
        if request.method not in _METODOS_LEITURA and not _pode_escrever(path):
            return JSONResponse(
                {"detail": "Apenas administradores podem alterar esta configuração."},
                status_code=403,
            )
        return None


def _iniciar_pipeline_bg(cfg: dict) -> None:
    """Executa em thread-pool — não bloqueia o loop asyncio."""
    try:
        pipeline.iniciar_cameras_db(cfg)
    except Exception as e:
        log.error("Falha ao iniciar pipelines: %s", e)


def _aquecer_modelos_bg(cfg: dict) -> None:
    """Carrega detector e OCR de leitura já no boot, em segundo plano.

    Sem isso, a PRIMEIRA leitura paga 45-60s de carregamento e parece travamento — e,
    pior, esse tempo já chegou a estourar o orçamento do laço e devolver erro. Aqui o
    custo cai numa janela em que ninguém está esperando resposta.
    """
    import time as _t
    try:
        from app.visao.detector import obter_detector_leitura
        from app.visao.ocr import obter_ocr_leitura
        t0 = _t.time()
        obter_detector_leitura(cfg)
        obter_ocr_leitura(cfg)
        log.info("Modelos de leitura prontos em %.1fs — primeira leitura já sai rápida",
                 _t.time() - t0)
    except Exception as e:
        # Falhar aqui não pode derrubar o servidor: a leitura recarrega sob demanda.
        log.warning("Não foi possível pré-carregar os modelos (%s) — serão carregados "
                    "na primeira leitura", e)


def _garantir_api_key(cfg: dict) -> dict:
    """Gera a api_key no primeiro boot se ela ainda não existir.

    `/api/leitura` deixou de ser público e passou a exigir a chave. Sem gerar uma aqui,
    uma instalação com `api_key` vazia ficaria com a integração do posto morta e sem
    pista do motivo — o roteador só veria 401. Gerando e logando em destaque, o caminho
    para consertar (copiar a chave para a configuração do roteador) fica explícito.
    """
    if cfg.get("api_key", "").strip():
        return cfg
    import secrets as _secrets
    chave = _secrets.token_urlsafe(32)
    cfg = {**cfg, "api_key": chave}
    config.salvar(cfg)
    log.warning(
        "api_key gerada agora (não havia nenhuma): %s\n"
        "  GET /api/leitura passou a exigir esta chave — configure-a no roteador do posto\n"
        "  como header 'X-API-Key' ou parâmetro '?api_key='.\n"
        "  Ela fica em %s e pode ser copiada em /configuracao → aba Sistema.",
        chave, config.CONFIG_PATH,
    )
    return cfg


@asynccontextmanager
async def lifespan(_app: FastAPI):
    cfg = config.carregar()
    logging.basicConfig(
        level=cfg["log_level"].upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    estado.instalar_log_handler()
    banco.inicializar()
    auth_mod.iniciar_cleanup()
    cfg = _garantir_api_key(cfg)

    # Registra o loop para o broadcaster poder empurrar eventos do pipeline
    loop = asyncio.get_running_loop()
    bc.broadcaster.registrar_loop(loop)

    # Inicia pipeline em background — servidor responde imediatamente
    _tarefa = loop.run_in_executor(None, _iniciar_pipeline_bg, cfg)
    # Aquece os modelos em paralelo, para a primeira leitura não pagar a carga
    _tarefa_modelos = loop.run_in_executor(None, _aquecer_modelos_bg, cfg)

    # Supervisor monitora threads de câmera e reinicia com backoff exponencial
    sv.supervisor.iniciar(cfg)

    # Retenção de dados: apaga deteccoes/chamadas/JPEGs antigos (retencao_dias=0 desativa)
    ret_mod.retencao.iniciar(config.get_int(cfg, "retencao_dias"))

    # DNS local embutido
    if config.get_bool(cfg, "dns_ativo"):
        dns_mod.dns_server.iniciar(
            hostname=cfg.get("dns_nome", "lpr.redesoft"),
            upstream=cfg.get("dns_upstream", "8.8.8.8"),
        )

    # HLS: inicia encoders FFmpeg se streaming_modo = hls
    if cfg.get("streaming_modo", "mjpeg") == "hls":
        cameras = banco.cameras_listar()
        if not hls_mod.hls_manager.iniciar(cameras):
            log.warning("HLS desativado — usando MJPEG como fallback")

    yield

    dns_mod.dns_server.parar()
    hls_mod.hls_manager.parar()
    sv.supervisor.parar()
    ret_mod.retencao.parar()
    pipeline.parar()
    await asyncio.shield(_tarefa)
    await asyncio.shield(_tarefa_modelos)


app = FastAPI(title="Leitura de Placas (ALPR)", lifespan=lifespan)
app.add_middleware(_AuthMiddleware)
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
_FOTOS_TESTE_DIR = "testes/fotos"
_CROPS_TESTE_DIR = "testes/resultados/crops"
import os as _os; _os.makedirs(_FOTOS_TESTE_DIR, exist_ok=True); _os.makedirs(_CROPS_TESTE_DIR, exist_ok=True)
app.mount("/testes/fotos", StaticFiles(directory=_FOTOS_TESTE_DIR), name="testes_fotos")
app.mount("/testes/resultados/crops", StaticFiles(directory=_CROPS_TESTE_DIR), name="testes_crops")
# HLS: diretório criado sob demanda pelo hls_manager; montado sempre para evitar
# erro de startup caso o modo seja ativado sem reiniciar o servidor.
_os.makedirs("hls", exist_ok=True)
app.mount("/hls", StaticFiles(directory="hls"), name="hls")
app.include_router(auth_rotas.router)
app.include_router(paginas.router)
app.include_router(stream_rotas.router)
app.include_router(api.router)
app.include_router(testes_rotas.router)
app.include_router(leitura_rotas.router)
app.include_router(cadastro_rotas.router)
app.include_router(usuarios_rotas.router)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    """Feed de detecções em tempo real. Requer sessão válida ou api_key."""
    # Autenticação dentro do endpoint (middleware já deixou passar /ws)
    token = websocket.cookies.get("sessao")
    autenticado = auth_mod.usuario_autenticado(token) is not None

    if not autenticado:
        cfg = config.carregar()
        api_key = cfg.get("api_key", "").strip()
        if api_key:
            autenticado = websocket.query_params.get("api_key", "") == api_key

    if not autenticado or banco.contar_usuarios() == 0:
        await websocket.close(code=1008)  # Policy Violation
        return

    await bc.broadcaster.conectar(websocket)
    try:
        while True:
            # Mantém a conexão viva; ignora mensagens do cliente
            await websocket.receive_text()
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        bc.broadcaster.desconectar(websocket)


def _ip_local() -> str:
    """Descobre o IP da LAN sem realmente abrir conexão."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _banner(porta: int) -> None:
    ip = _ip_local()
    cfg = config.carregar()
    linhas = [
        "",
        "=" * 64,
        "  Leitura de Placas (ALPR) - servidor iniciando",
        "",
        f"  Local:         http://localhost:{porta}",
        f"  Rede:          http://{ip}:{porta}",
    ]
    if config.get_bool(cfg, "dns_ativo"):
        nome = cfg.get("dns_nome", "lpr.redesoft")
        linhas.append(f"  DNS local:     http://{nome}  →  {ip}")
    linhas += [
        "",
        f"  Ao Vivo:       http://localhost:{porta}/",
        f"  Dashboard:     http://localhost:{porta}/dashboard",
        f"  Configuracao:  http://localhost:{porta}/configuracao",
        f"  Historico:     http://localhost:{porta}/historico",
        f"  Listas:        http://localhost:{porta}/listas",
        "=" * 64,
        "",
    ]
    print("\n".join(linhas), flush=True)


def _porta_livre(porta: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", porta)) != 0


def iniciar(reload: bool = False) -> None:
    cfg = config.carregar()
    porta = config.get_int(cfg, "porta")
    if not _porta_livre(porta):
        print(
            f"\n  ERRO: porta {porta} já está em uso.\n"
            f"  Feche a instância anterior e tente novamente.\n"
            f"  (Windows: taskkill /F /IM python.exe)\n",
            flush=True,
        )
        sys.exit(1)
    _banner(porta)
    raiz = str(Path(__file__).resolve().parent.parent)
    if reload:
        print("  Modo --reload ativo: reinicia ao detectar alterações em .py e .html\n", flush=True)
        uvicorn.run(
            "app.servidor:app",
            host="0.0.0.0",
            port=porta,
            log_level=cfg["log_level"].lower(),
            reload=True,
            reload_dirs=[raiz],
            reload_includes=["*.py", "*.html"],
            reload_excludes=[".venv", "__pycache__", "testes", "*.pyc"],
        )
    else:
        uvicorn.run(app, host="0.0.0.0", port=porta, log_level=cfg["log_level"].lower())
