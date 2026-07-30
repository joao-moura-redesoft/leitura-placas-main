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

# Garante MIME types corretos para servir arquivos HLS via StaticFiles
mimetypes.add_type("application/vnd.apple.mpegurl", ".m3u8")
mimetypes.add_type("video/mp2t", ".ts")


log = logging.getLogger(__name__)

# Rotas que não exigem autenticação via middleware
# /ws é público no middleware; a autenticação é feita dentro do endpoint.
# /api/leitura: sem auth por enquanto (rede interna do sidecar Java do posto, não exposta
# ao público) — trocar pra api_key depois é só remover daqui, o mecanismo já existe abaixo.
# /api/healthz: liveness do container (só {"status":"ok"}, sem dado de cliente). O
# /api/health detalhado, com nome de câmera/posto, continua exigindo autenticação.
_PUBLICAS = frozenset({"/login", "/criar-admin", "/favicon.ico", "/ws",
                       "/api/leitura", "/api/healthz"})


class _AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Arquivos estáticos e streams sempre públicos
        if (path.startswith("/static/") or path.startswith("/testes/fotos/")
                or path.startswith("/testes/resultados/")
                or path in _PUBLICAS):
            return await call_next(request)

        # Sem usuários → redireciona para criar o primeiro admin
        if banco.contar_usuarios() == 0:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Servidor não configurado. Acesse /criar-admin."}, status_code=503)
            return RedirectResponse("/criar-admin", status_code=303)

        # Autenticação via cookie de sessão
        token = request.cookies.get("sessao")
        if token and auth_mod.obter_user_id(token) is not None:
            return await call_next(request)

        # Autenticação via api_key (para integrações externas sem browser)
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


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    """Feed de detecções em tempo real. Requer sessão válida ou api_key."""
    # Autenticação dentro do endpoint (middleware já deixou passar /ws)
    token = websocket.cookies.get("sessao")
    autenticado = bool(token and auth_mod.obter_user_id(token) is not None)

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
