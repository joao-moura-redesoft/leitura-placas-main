"""Aplicação FastAPI — inicializa banco, pipeline e rotas."""
from __future__ import annotations
import asyncio
import logging
import platform
import re
import secrets
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Deve ficar aqui (e não só em main.py) porque em modo --reload o uvicorn
# importa "app.servidor:app" num subprocess separado, sem passar pelo main.py.
if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Mesma razão de estar aqui, e não em main.py nem no `lifespan`: o subprocess do --reload
# não passa pelo main.py, e o `lifespan` roda DEPOIS de `app.visao` ter sido importado. Isto
# configura estado global de biblioteca nativa e só vale se acontecer antes do primeiro
# frame — ver `app/core/nativo.py`, que documenta as 1030 exceções de OpenCV que a ausência
# disto custou num único processo.
from app.core import nativo    # noqa: E402  (tem de vir antes de qualquer import de visao)

nativo.aplicar()

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
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
from app.visao import contexto_log, pipeline
from app.web import api, paginas
from app.web import auth as auth_rotas
from app.web import cadastro as cadastro_rotas
from app.web import deps as web_deps
from app.seguranca import limitador
from app.web import leitura as leitura_rotas
from app.web import stream as stream_rotas
from app.web import testes as testes_rotas
from app.web import usuarios as usuarios_rotas

# Garante MIME types corretos para servir arquivos HLS via StaticFiles
mimetypes.add_type("application/vnd.apple.mpegurl", ".m3u8")
mimetypes.add_type("video/mp2t", ".ts")
# Fontes: o Windows não registra .woff2 e o StaticFiles cai em
# application/octet-stream. Os <link rel="preload" as="font" type="font/woff2">
# do base.html/auth_base.html são descartados quando o Content-Type não bate, e
# o browser baixa a fonte de novo — o preload vira download duplicado.
mimetypes.add_type("font/woff2", ".woff2")


log = logging.getLogger(__name__)

# Rotas que não exigem autenticação via middleware
# /ws é público no middleware; a autenticação é feita dentro do endpoint.
# /api/leitura: sem auth por enquanto (rede interna do sidecar Java do posto, não exposta
# ao público) — trocar pra api_key depois é só remover daqui, o mecanismo já existe abaixo.
# /api/healthz: liveness do container (só {"status":"ok"}, sem dado de cliente). O
# /api/health detalhado, com nome de câmera/posto, continua exigindo autenticação.
_PUBLICAS = frozenset({"/login", "/criar-admin", "/favicon.ico", "/ws",
                       "/api/leitura", "/api/healthz", "/esqueci-senha"})

# Preview de um bico: continua exigindo credencial (o arquivo é privado — ver
# app/visao/leitura.py:PREVIEW_DIR), mas ACEITA a api_key própria do posto dono do bico,
# além da sessão do painel e da api_key global. Sem isso, `/api/leitura` (público)
# devolvia em `frame_url` uma URL que o próprio chamador não conseguia buscar: o roteador
# recebe a placa e não consegue mostrar a foto ao atendente, que é justamente o fluxo
# recomendado quando `confirmada` vem false.
_RE_PREVIEW_BICO = re.compile(r"^/api/bicos/(\d+)/preview\.jpg$")


# Tentativas de api_key global por IP, por minuto. Generoso para não atrapalhar integração
# legítima (que acerta a chave e nem chega a contar duas vezes), e baixo o bastante para que
# adivinhar deixe de ser gratuito.
_LIMITE_APIKEY_MIN = 30


def _limite_ou_429(bucket: str, ip: str, rotulo_log: str) -> JSONResponse | None:
    """Aplica o freio de `_LIMITE_APIKEY_MIN`/min a `bucket`+`ip`. `None` = liberado;
    senão, a resposta 429 pronta para devolver.

    Compartilhado entre a `api_key` global e a chave de preview por posto — os dois
    bloqueios de `_AuthMiddleware.dispatch` faziam a mesma checagem, log e resposta,
    com só o nome do bucket/rótulo do log mudando. (Achado A5, review de 28/08/2026.)
    """
    if limitador.permitido(bucket, ip, _LIMITE_APIKEY_MIN, 60):
        return None
    log.warning("%s: limite de tentativas excedido para o IP %s", rotulo_log, ip)
    return JSONResponse({"detail": "Muitas tentativas — aguarde um instante."},
                        status_code=429)


def _ampliar_threadpool(total: int) -> None:
    """Aumenta o limite de threads que as rotas síncronas compartilham.

    O AnyIO expõe isso por um `CapacityLimiter` de processo. A API é semi-privada e mudou
    entre versões, então a falha aqui NUNCA pode derrubar o boot: sem o ajuste o servidor
    roda com os 40 do default, que é o comportamento anterior.
    """
    if total <= 0:
        return
    try:
        import anyio.to_thread
        anyio.to_thread.current_default_thread_limiter().total_tokens = total
        log.info("Threadpool de rotas síncronas: %d threads", total)
    except Exception as e:
        log.warning("Não foi possível ampliar o threadpool (%s) — seguindo com o default "
                    "do AnyIO (40). Streams MJPEG simultâneos podem esgotá-lo.", e)


class _AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Assets do painel (CSS, JS, fontes, ícones) são públicos — não têm dado de cliente.
        #
        # `/static/snapshots/` NÃO entra: são fotos de veículo de cliente real, nomeadas
        # `{timestamp}_{PLACA}.jpg`. Quem soubesse a placa e a janela de tempo varria a pasta
        # por força bruta, sem login, sem rate limit. O projeto já registrava o risco em
        # comentário (`app/visao/leitura.py`), e a mitigação aplicada ao preview — mover para
        # `dados_privados/` — nunca foi estendida ao histórico. Mesma coisa para
        # `/testes/fotos/` e `/testes/resultados/`: o router `/api/testes` é admin-only, mas
        # o conteúdo que ele grava estava sendo servido a anônimos, e ali há recorte de ROI
        # de bico de cliente. (Auditoria 27/08/2026, achado A4.)
        publico_estatico = (
            path.startswith("/static/") and not path.startswith("/static/snapshots/")
        )
        if (publico_estatico
                # /redefinir-senha/{token}: quem chega aqui, por definição, não tem
                # como estar logado (perdeu a senha) — o token no caminho É a prova
                # de identidade desta rota, não a sessão.
                or path.startswith("/redefinir-senha/")
                or path in _PUBLICAS):
            return await call_next(request)

        # Sem usuários → redireciona para criar o primeiro admin
        if banco.contar_usuarios() == 0:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Servidor não configurado. Acesse /criar-admin."}, status_code=503)
            return RedirectResponse("/criar-admin", status_code=303)

        # Autenticação via cookie de sessão. Guarda o usuário em `request.state.user`
        # para as rotas diferenciarem admin de 'cliente' (app/web/deps.py) sem cada
        # uma ter que ir buscar no banco de novo.
        token = request.cookies.get("sessao")
        if token:
            user_id = auth_mod.obter_user_id(token)
            if user_id is not None:
                user = banco.buscar_usuario_id(user_id)
                if user is not None and user["ativo"]:
                    request.state.user = user
                    return await call_next(request)

        # Autenticação via api_key (para integrações externas sem browser).
        #
        # Três correções da auditoria de 27/08/2026 (achado A5), todas no mesmo ponto:
        #
        # 1. `secrets.compare_digest` no lugar de `==`. Esta chave vale para TUDO, inclusive
        #    /api/config e /api/usuarios — e dez linhas abaixo a chave do POSTO, de escopo
        #    muito menor, já era comparada assim. O padrão existia e não tinha sido aplicado
        #    justamente à mais poderosa.
        # 2. Rate limit por IP. Não havia NENHUM freio aqui: dava para martelar chaves
        #    candidatas de graça, e cada tentativa ainda custava um `config.carregar()` (I/O
        #    de disco) ao servidor. O módulo `limitador` já existia e só era usado em
        #    /api/leitura.
        # 3. O `carregar()` só acontece depois do freio, para a tentativa recusada não pagar
        #    o disco.
        ip_req = request.client.host if request.client else "?"
        enviada_global = (request.headers.get("X-API-Key", "")
                          or request.query_params.get("api_key", ""))
        if enviada_global:
            recusa = _limite_ou_429("api_key_global", ip_req, "api_key")
            if recusa is not None:
                return recusa
            cfg = config.carregar()
            api_key = cfg.get("api_key", "").strip()
            if api_key and secrets.compare_digest(enviada_global, api_key):
                return await call_next(request)

        # Chave PRÓPRIA do posto, só para o preview daquele bico (escopo estreito de
        # propósito: a chave do posto A nunca abre o preview do posto B).
        m = _RE_PREVIEW_BICO.match(path)
        if m:
            bico_id = int(m.group(1))
            enviada = (request.headers.get("X-API-Key", "")
                       or request.query_params.get("api_key", ""))
            if enviada:
                # Mesmo freio da api_key global (achado A5): sem isto dava para martelar
                # candidatas de graça contra a chave do posto — ela tinha a comparação em
                # tempo constante, mas nenhum teto de tentativas.
                recusa = _limite_ou_429(f"preview_bico_{bico_id}", ip_req,
                                        f"preview do bico {bico_id}")
                if recusa is not None:
                    return recusa
                chave_posto = web_deps.chave_do_posto_do_bico(bico_id)
                if chave_posto and secrets.compare_digest(enviada, chave_posto):
                    return await call_next(request)

        # Não autenticado
        if path.startswith("/api/") or path.startswith("/stream/"):
            return JSONResponse({"detail": "Não autenticado."}, status_code=401)
        return RedirectResponse("/login", status_code=303)


class _SegurancaMiddleware(BaseHTTPMiddleware):
    """Headers de defesa em profundidade contra XSS de origem externa, clickjacking
    e MIME-sniffing.

    O CSP libera 'unsafe-inline' em script/style porque o frontend hoje usa
    onclick="" e <style>/style="" inline por toda parte — apertar mais que isso
    quebraria a aplicação inteira sem antes migrar esses padrões (ver avaliação
    de frontend). Mesmo com 'unsafe-inline', já fecha a porta de scripts/estilos
    carregados de fora (ex.: um payload de XSS tentando <script src="https://
    evil.com/x.js">, ou uma dependência de CDN comprometida) e de o site ser
    embutido em iframe de outra origem (clickjacking).
    """

    async def dispatch(self, request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "media-src 'self' blob:; "
            "connect-src 'self' ws: wss:; "
            "font-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
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
        from app.visao.detector import obter_detector_leitura, obter_detector_rapido
        from app.visao.ocr import obter_ocr_leitura, obter_ocr_rapido
        t0 = _t.time()
        obter_detector_leitura(cfg)
        obter_ocr_leitura(cfg)
        log.info("Modelos de leitura prontos em %.1fs — primeira leitura já sai rápida",
                 _t.time() - t0)
        # O perfil rápido tem par PRÓPRIO de modelos, e sem aquecê-los aqui a primeira
        # chamada com `rapido=1` pagaria a carga inteira — a latência que o modo existe
        # para evitar, no pior momento possível. Depois do par completo de propósito:
        # este é o perfil opcional, e o completo é o que atende quem não pediu nada.
        if config.get_bool(cfg, "rapido_ativo"):
            t1 = _t.time()
            obter_detector_rapido(cfg)
            obter_ocr_rapido(cfg)
            log.info("Modelos do perfil rápido prontos em %.1fs", _t.time() - t1)
    except Exception as e:
        # Falhar aqui não pode derrubar o servidor: a leitura recarrega sob demanda.
        log.warning("Não foi possível pré-carregar os modelos (%s) — serão carregados "
                    "na primeira leitura", e)


# Endpoints consultados em laço, sem ninguém pedir: os três primeiros a interface
# atualiza sozinha, o último é o healthcheck do Docker a cada 30s (ver Dockerfile). O
# access log do uvicorn repete cada um a cada volta, com formato próprio (sem timestamp,
# sem nível), e `/api/logs` é o caso extremo — a tela de logs enche de ruído justamente
# o log que ela está exibindo. Só o poll BEM-SUCEDIDO some: 4xx/5xx continuam aparecendo,
# que é quando a linha informa alguma coisa (healthcheck falhando é o que derruba o
# container, e precisa estar no log).
_ROTAS_POLLING = frozenset((
    "/api/logs", "/api/chamadas", "/api/chamadas/resumo", "/api/healthz",
))


class _FiltroPolling(logging.Filter):
    """Descarta a linha de access log dos pollings de rotina.

    O record do uvicorn.access traz `args = (cliente, método, caminho, versão, status)`;
    tudo que não tiver essa forma passa intacto, para que uma mudança no formato do
    uvicorn silencie no máximo nada — nunca o log inteiro.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Em DEBUG quem ligou o nível quer ver TUDO, inclusive o poll.
        if logging.getLogger().getEffectiveLevel() <= logging.DEBUG:
            return True
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        try:
            if int(args[4]) >= 400:
                return True
        except (TypeError, ValueError):
            return True
        return str(args[2]).split("?")[0] not in _ROTAS_POLLING


def _silenciar_polling_da_ui() -> None:
    logging.getLogger("uvicorn.access").addFilter(_FiltroPolling())


def _instalar_log_arquivo(cfg: dict) -> None:
    """Log em arquivo com rotação, mais o dump do `faulthandler` no MESMO arquivo.

    Sem isto, "a aplicação caiu do nada" é impossivel de investigar: o `basicConfig`
    escrevia só em stderr, e `/api/logs` lê um buffer em memória que morre com o processo.
    O `servidor.log` que existe hoje só existe porque quem sobe o servidor redireciona a
    saída à mão - numa máquina onde ninguém redirecionou, não há nada para ler.

    `faulthandler` importa mais que o log Python nas quedas que interessam: quando o
    processo morre por access violation do OpenCV/FFmpeg não existe traceback Python, e o
    dump nativo é a única pista. Ele estava ATIVO no log de 24/08 (2061 dumps) por acidente
    de ambiente - não há nenhum `faulthandler` em `app/` -, o que significa que noutra
    máquina aqueles 2061 dumps não teriam sido gravados. Aqui passa a ser explicito.

    NUNCA levanta: log é diagnóstico, e um disco cheio ou caminho sem permissão não pode
    impedir o servidor de subir.
    """
    caminho = str(cfg.get("log_arquivo", "") or "").strip()
    if not caminho:
        return
    try:
        from logging.handlers import RotatingFileHandler

        # Caminho com pasta ("logs/alpr.log") tem de funcionar sem `mkdir` manual: no posto
        # ninguem vai criar diretorio para o log subir.
        pai = Path(caminho).parent
        if str(pai) not in ("", "."):
            pai.mkdir(parents=True, exist_ok=True)
        mb = max(1, config.get_int(cfg, "log_arquivo_mb"))
        backups = max(0, config.get_int(cfg, "log_arquivo_backups"))
        h = RotatingFileHandler(caminho, maxBytes=mb * 1024 * 1024,
                                backupCount=backups, encoding="utf-8")
        h.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logging.getLogger().addHandler(h)
    except Exception as e:
        log.warning("Log em arquivo desativado (%s): %s", caminho, e)
        return

    try:
        import faulthandler

        # `delete=False`/append: o dump nativo tem de sobreviver à próxima subida, senão o
        # reinicio automatico apaga a evidência da queda que causou o reinicio.
        # `-nativo.log` e nao `.nativo`: o `.gitignore` do projeto cobre `*.log`, e um
        # dump com placa e caminho de cliente nao pode escapar por causa da extensao.
        nativo_path = str(Path(caminho).with_suffix("")) + "-nativo.log"
        _fh = open(nativo_path, "a", buffering=1, encoding="utf-8")
        faulthandler.enable(file=_fh, all_threads=True)
        # Guardado no módulo para o arquivo não ser fechado pelo GC: o faulthandler grava
        # nele de dentro de um handler de sinal, e escrever num descritor fechado ali é uma
        # segunda falha nativa em cima da primeira.
        globals()["_ARQUIVO_FAULTHANDLER"] = _fh
        log.info("Log em arquivo: %s (%d MB x %d) + dump nativo em %s",
                 caminho, mb, backups, nativo_path)
    except Exception as e:
        log.warning("faulthandler não instalado: %s", e)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    cfg = config.carregar()
    logging.basicConfig(
        level=cfg["log_level"].upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    _instalar_log_arquivo(cfg)
    # Só agora há handler: `nativo.aplicar()` rodou no import deste módulo, muito antes.
    log.info("OpenCV blindado: %s", nativo.estado_para_log())
    _silenciar_polling_da_ui()
    contexto_log.instalar()
    estado.instalar_log_handler()
    banco.inicializar()
    auth_mod.iniciar_cleanup()

    # Registra o loop para o broadcaster poder empurrar eventos do pipeline
    loop = asyncio.get_running_loop()
    bc.broadcaster.registrar_loop(loop)

    # Aquece os modelos e SO DEPOIS sobe o pipeline. Os dois rodavam em paralelo, e o
    # paralelismo custava caro: carregar sessao ONNX / importar EasyOCR num thread enquanto
    # outro roda CLAHE, bilateralFilter e inferencia poe o OpenCV num estado em que a
    # chamada seguinte estoura. Medido no log de 24/08/2026: 849 falhas do `VehicleDetector`
    # e 846 do `AjustadorAmbiente` com "Unknown C++ exception from OpenCV code", todas numa
    # janela de 3,5 min que comeca 19 ms depois de `VehicleDetector carregado` e nao volta
    # nas 2h16 seguintes. Nessa janela o tipo do veiculo chegou nulo ao banco, em silencio.
    #
    # O custo de sequenciar e alguns segundos a mais para a primeira camera comecar a
    # detectar. Isso e barato: o servidor ja responde (as duas tarefas estao fora do loop),
    # e a leitura reativa do bico carrega o que precisa sob demanda de qualquer jeito.
    async def _subir_visao() -> None:
        await loop.run_in_executor(None, _aquecer_modelos_bg, cfg)
        await loop.run_in_executor(None, _iniciar_pipeline_bg, cfg)

    _tarefa_visao = asyncio.ensure_future(_subir_visao())

    # Threadpool do AnyIO: 40 (o default) é pouco para este servidor.
    #
    # Medido: 114 rotas são `def` síncrono contra 5 `async`, então TODA requisição consome
    # um token. Pior, cada viewer de MJPEG segura o seu quase 100% do tempo — o gerador é
    # síncrono e dorme dentro do passo (`app/streaming/stream.py`). Somando abas de painel
    # abertas com `POST /api/config` (que reinicia o pipeline na própria thread da request)
    # e `ler-placa-teste` (28 s cada), o pool esgotava e o servidor inteiro parava de
    # responder — inclusive `/api/leitura`, que é o faturamento, e `/api/healthz`, o que
    # fazia o orquestrador reiniciar o container no meio de um restart de câmera.
    # (Auditoria 27/08/2026, achado K5.)
    #
    # Ampliar o pool é a metade barata da correção; a outra metade é `_LIMITE_LER_PLACA_MIN`
    # em `app/web/cadastro.py`, que impede uma única sessão de consumir tudo sozinha.
    _ampliar_threadpool(config.get_int(cfg, "threadpool_max"))

    # Supervisor monitora threads de câmera e reinicia com backoff exponencial
    sv.supervisor.iniciar(cfg)

    # Retenção de dados, duas políticas no mesmo worker: por PRAZO apaga deteccoes/chamadas/
    # JPEGs antigos (retencao_dias=0 desativa); por CONTAGEM tira só a foto das leituras que
    # passaram do teto, mantendo a linha (retencao_max_imagens=0 desativa).
    ret_mod.retencao.iniciar(
        config.get_int(cfg, "retencao_dias"),
        config.get_int(cfg, "retencao_max_imagens"),
    )

    # Coleta de imagens para o dataset de testes (captura_dataset=nao desativa). Vive
    # fora do Pipeline de propósito: com `deteccao_automatica=nao` — o caso comum, já que
    # a leitura é reativa ao bico — o Pipeline só dorme, e nada seria coletado.
    from app.visao import captura_dataset as cap_mod
    cap_mod.iniciar_coletor(cfg)

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
    # O supervisor ANTES do pipeline, e com join: ele é o único que pode CRIAR pipeline
    # enquanto tudo desce. Parado só por `Event.set()`, um `reiniciar_camera` em voo
    # registrava a instância depois de `pipeline.parar()` e o processo saía com `cap.read()`
    # em andamento — o access violation que `Camera.fechar()` existe para evitar.
    sv.supervisor.parar()
    ret_mod.retencao.parar()
    # O coletor abre RTSP por conta própria e segura `lock_camera`: tem de morrer ANTES de
    # `pipeline.parar()`, senão ele reabre a câmera que o pipeline acabou de fechar.
    # `parar_coletor` não era chamado em lugar nenhum do projeto, nem aqui.
    from app.visao import captura_dataset as _cap
    _cap.parar_coletor()
    pipeline.parar()
    # Uma tarefa só agora: aquecer-modelos e subir-pipeline viraram uma sequência (ver
    # `_subir_visao`). Aguardar aqui é o que garante que nenhum thread de visao continua
    # mexendo em camera depois de `pipeline.parar()` - a corrida que `Camera.fechar()` já
    # documenta como causa de access violation.
    await asyncio.shield(_tarefa_visao)


app = FastAPI(title="Leitura de Placas (ALPR)", lifespan=lifespan)
# ORDEM IMPORTA e é contra-intuitiva: `add_middleware` INSERE NO INÍCIO da pilha, então o
# último adicionado é o mais EXTERNO. Com Segurança primeiro e Auth depois, o Auth ficava por
# fora e os 401/303 que ele mesmo emite saíam sem CSP, sem `X-Frame-Options` e sem `nosniff`.
# Invertendo, Segurança envolve tudo — inclusive as respostas do Auth. (Auditoria 27/08/2026.)
app.add_middleware(_AuthMiddleware)
app.add_middleware(_SegurancaMiddleware)
class _EstaticosApp(StaticFiles):
    """StaticFiles que obriga o navegador a revalidar CSS e JS.

    Sem `Cache-Control`, o navegador aplica cache heurístico: guarda o arquivo por
    uma fração da idade dele e serve da memória SEM perguntar ao servidor. Numa
    atualização do sistema isso entrega a página nova com o base.css/app.js VELHOS —
    o sintoma é layout quebrado e botão que não responde, porque o HTML chama uma
    função que a versão em cache ainda não tem. Não é hipótese: aconteceu.

    `no-cache` não desliga o cache, só exige revalidação — a resposta normal é um
    304 sem corpo. Snapshot e fonte ficam de fora de propósito: têm nome único (ou
    nunca mudam) e são o volume de verdade.
    """

    async def get_response(self, path: str, scope):
        resposta = await super().get_response(path, scope)
        if path.endswith((".css", ".js")):
            resposta.headers["Cache-Control"] = "no-cache"
        return resposta


app.mount("/static", _EstaticosApp(directory="app/web/static"), name="static")
_FOTOS_TESTE_DIR = "testes/fotos"
_CROPS_TESTE_DIR = "testes/resultados/crops"
import os as _os; _os.makedirs(_FOTOS_TESTE_DIR, exist_ok=True); _os.makedirs(_CROPS_TESTE_DIR, exist_ok=True)
app.mount("/testes/fotos", StaticFiles(directory=_FOTOS_TESTE_DIR), name="testes_fotos")
app.mount("/testes/resultados/crops", StaticFiles(directory=_CROPS_TESTE_DIR), name="testes_crops")
# HLS: diretório criado sob demanda pelo hls_manager; montado sempre para evitar
# erro de startup caso o modo seja ativado sem reiniciar o servidor.
#
# `_HlsPorPosto` e não `StaticFiles` puro: os segmentos ficam em `hls/{camera_id}/`, e um
# mount cru só exigia LOGIN — nada escopava por empresa. Um `cliente` do posto 4 pedia
# `/hls/9/index.m3u8` e recebia o vídeo ao vivo da câmera do posto 7; `camera_id` é inteiro
# sequencial pequeno, então nem adivinhação era preciso. O caminho MJPEG
# (`app/web/stream.py`) já fazia a checagem certa. (Auditoria 27/08/2026, achado A4.)
_os.makedirs("hls", exist_ok=True)


class _HlsPorPosto(StaticFiles):
    """StaticFiles que confere o dono da câmera antes de servir playlist ou segmento."""

    _RE_CAM = re.compile(r"^/?(\d+)/")

    async def get_response(self, path: str, scope):
        m = self._RE_CAM.match(path.replace("\\", "/"))
        if m is None:
            # Nada fora de `hls/{id}/...` é servível — inclusive o índice do diretório.
            return JSONResponse({"detail": "Não encontrado."}, status_code=404)
        request = Request(scope)
        # Cacheado: sem isto, cada segmento .ts (buscado a cada poucos segundos por
        # câmera por espectador) fazia um SELECT novo — ver web_deps.empresa_da_camera_cacheada.
        empresa_id = web_deps.empresa_da_camera_cacheada(int(m.group(1)))
        if empresa_id is web_deps._AUSENTE:
            return JSONResponse({"detail": "Câmera não encontrada."}, status_code=404)
        try:
            web_deps.checar_acesso_empresa(request, empresa_id)
        except HTTPException as e:
            # `checar_acesso_empresa` levanta 404 DE PROPÓSITO (não confirmar a um cliente
            # fora do escopo que a câmera existe) — repassar o status REAL em vez de um 403
            # fixo é o que fecha o oráculo de enumeração 403≠existe / 404≠não-existe.
            return JSONResponse({"detail": e.detail}, status_code=e.status_code)
        except Exception as e:
            log.error("Erro ao checar acesso HLS da câmera %s: %s", m.group(1), e)
            return JSONResponse({"detail": "Erro interno."}, status_code=500)
        return await super().get_response(path, scope)


app.mount("/hls", _HlsPorPosto(directory="hls"), name="hls")
app.include_router(auth_rotas.router)
app.include_router(paginas.router)
app.include_router(stream_rotas.router)
app.include_router(api.router)
# Ferramenta interna de QA (dataset de acurácia OCR) — sem uso para um cliente,
# admin-gate no router inteiro em vez de repetir a dependency rota a rota.
app.include_router(testes_rotas.router, dependencies=[Depends(web_deps.exigir_admin)])
app.include_router(leitura_rotas.router)
app.include_router(cadastro_rotas.router)
app.include_router(usuarios_rotas.router)  # cada rota decide sozinha (ver app/web/usuarios.py)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    """Feed de detecções em tempo real. Requer sessão de ADMIN ou api_key.

    Não é escopado por empresa (o broadcaster empurra detecções de TODAS as câmeras
    do processo, ver app/core/broadcaster.py) — é o mecanismo do modo contínuo/diagnóstico
    visual, não do fluxo reativo multi-tenant. Por isso fica restrito a admin: um usuário
    'cliente' conectado aqui veria detecções de outros clientes.
    """
    token = websocket.cookies.get("sessao")
    user_id = auth_mod.obter_user_id(token) if token else None
    user = banco.buscar_usuario_id(user_id) if user_id is not None else None
    autenticado = user is not None and user["ativo"] and user["papel"] == "admin"

    if not autenticado:
        cfg = config.carregar()
        api_key = cfg.get("api_key", "").strip()
        enviada = websocket.query_params.get("api_key", "")
        # Mesma chave global do middleware: mesma comparação constant-time (achado A5).
        if api_key and enviada:
            autenticado = secrets.compare_digest(enviada, api_key)

    if not autenticado or banco.contar_usuarios() == 0:
        await websocket.close(code=1008)  # Policy Violation
        return

    await bc.broadcaster.conectar(websocket)
    try:
        while True:
            # Mantém a conexão viva; ignora mensagens do cliente
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass          # desconexão do cliente é o fim NORMAL deste laço
    except Exception as e:
        # Engolir tudo sem log escondia falha real do broadcaster. (Auditoria 27/08/2026.)
        log.warning("WebSocket encerrado por erro: %s", e, exc_info=True)
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
