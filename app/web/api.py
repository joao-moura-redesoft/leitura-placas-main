"""API REST para detecções, listas e status."""
from __future__ import annotations
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

from app.core import banco
from app.integracoes import apiplacas
from app.core import config
from app.core import estado
from app.core import rotulos
from app.operacao import retencao as ret_mod
from app.operacao import supervisor as sv
from app.visao import camera as camera_mod
from app.visao import leitura
from app.visao import pipeline
from app.web import deps
from app.web import redacao

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def _iniciar_camera_bg(camera_db_id: int, cam_cfg: dict) -> None:
    try:
        pipeline.iniciar_camera(camera_db_id, cam_cfg)
    except Exception as e:
        log.error("Falha ao iniciar câmera %d: %s", camera_db_id, e)


def _iniciar_coletor_bg(cfg: dict) -> None:
    try:
        from app.visao import captura_dataset as cap_mod
        cap_mod.iniciar_coletor(cfg)
    except Exception as e:
        log.error("Falha ao aplicar a nova config no coletor de dataset: %s", e)


def _empresa_efetiva(request: Request, empresa_id: int | None) -> int | None:
    """Reconcilia o `empresa_id` pedido na query com o escopo do usuário logado: admin
    usa o que veio na query (ou None = sem filtro); 'cliente' é sempre forçado ao
    próprio posto, mesmo que peça outro — não confiamos no valor vindo do cliente."""
    escopo = deps.empresa_do_usuario(request)
    return escopo if escopo is not None else empresa_id


@router.get("/deteccoes")
def listar_deteccoes(
    request: Request,
    placa: str | None = None,
    desde: str | None = None,
    ate: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0, le=200_000),
    empresa_id: int | None = None,
    bico_id: int | None = None,
    incluir_testes: bool = False,
    origem: Literal["producao", "teste", "todas"] | None = None,
    tipo_veiculo: Literal["moto", "carro", "desconhecido", "todos"] | None = None,
):
    """`origem` filtra por conjunto: 'producao' (default — exclui testes manuais),
    'teste' (só eles) ou 'todas'. `incluir_testes` é o parâmetro antigo equivalente a
    'todas'; continua aceito, mas `origem` tem precedência quando os dois vêm.

    `tipo_veiculo` filtra moto/carro. É a ESTIMATIVA do detector de veículo gravada na
    leitura, não um cadastro: 'desconhecido' traz as leituras sem estimativa (2 estágios
    desligado, nenhum veículo detectado no quadro, ou anteriores à troca de fonte) e o
    default traz todas."""
    empresa_id = _empresa_efetiva(request, empresa_id)
    return banco.listar_deteccoes(placa=placa, desde=desde, ate=ate, limit=limit,
                                  offset=offset, empresa_id=empresa_id, bico_id=bico_id,
                                  incluir_testes=incluir_testes, origem=origem,
                                  tipo_veiculo=tipo_veiculo)


@router.get("/chamadas")
def chamadas_listar(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    empresa_id: int | None = None,
    status: str | None = None,
    apenas_erros: bool = False,
):
    """Chamadas do roteador ao endpoint reativo — inclusive as recusadas."""
    empresa_id = _empresa_efetiva(request, empresa_id)
    return banco.chamadas_listar(limit=limit, empresa_id=empresa_id,
                                 status=status, apenas_erros=apenas_erros)


@router.get("/chamadas/resumo")
def chamadas_resumo(request: Request, horas: int = Query(24, ge=1, le=720)):
    return banco.chamadas_resumo(horas=horas, empresa_id=deps.empresa_do_usuario(request))


@router.delete("/deteccoes/{id_}", dependencies=[Depends(deps.exigir_admin)])
def remover_deteccao(id_: int):
    # `is None` e não `if not`: a lista de arquivos vem VAZIA quando a detecção existia mas
    # não tinha foto (snapshot desligado, ou o teto de contagem já a tirou), e `not []` é
    # verdadeiro — com `if not` essa remoção legítima virava 404 depois de já ter apagado
    # a linha.
    arquivos = banco.remover_deteccao(id_)
    if arquivos is None:
        raise HTTPException(404, "Detecção não encontrada")
    # Mesma proteção que `_purgar_por_contagem` (retencao.py) já aplica em lote: um
    # snapshot também rotulado em testes/dataset.json não pode ser apagado — a pasta é
    # gitignored e não tem cópia. `apagar_orfaos` não checa rótulo por padrão de
    # propósito (só é seguro para a absorção do pipeline, com janela curta) — aqui é
    # remoção manual de uma linha do histórico, sem essa janela de segurança.
    intocaveis = rotulos.protegidos()
    if intocaveis is None:
        log.warning("Remoção da detecção %d: dataset ilegível — snapshots preservados", id_)
        apagaveis: list[str] = []
    else:
        apagaveis = [a for a in arquivos if Path(a).name not in intocaveis]
    # Sem isto o JPEG ficava em disco para sempre: nenhuma limpeza automática o alcança
    # depois, porque todas partem do banco e a linha acabou de sumir.
    ret_mod.apagar_orfaos(apagaveis)
    return {"removido": True}


@router.get("/stats", dependencies=[Depends(deps.exigir_admin)])
def stats():
    cfg = config.carregar()
    return {
        **banco.stats(fuso=cfg.get("fuso_horario", "America/Sao_Paulo")),
        "fps": estado.fps_atual(),
        "uptime_seg": estado.uptime_segundos(),
        "pipeline": estado.pipeline_rodando(),
        # Por câmera, além do agregado: com várias câmeras, um booleano só não diz qual
        # parou — e era o que a tela mostrava (achado M9).
        "por_camera": estado.cameras_no_ar(),
        "deteccao_automatica": cfg.get("deteccao_automatica", "sim").lower() in ("sim", "true", "1"),
        "streaming_modo": cfg.get("streaming_modo", "mjpeg"),
    }


@router.get("/logs", dependencies=[Depends(deps.exigir_admin)])
def logs(nivel: str | None = None, limit: int = Query(100, ge=1, le=200)):
    todos = estado.listar_logs()
    if nivel:
        nivel = nivel.upper()
        todos = [l for l in todos if l["level"] == nivel]
    return todos[:limit]


@router.delete("/logs", dependencies=[Depends(deps.exigir_admin)])
def limpar_logs():
    estado.limpar_logs()
    return {"limpo": True}


@router.get("/auditoria", dependencies=[Depends(deps.exigir_admin)])
def auditoria(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, le=200_000),
    acao: str | None = None,
    usuario_id: int | None = None,
):
    """Quem fez o quê no painel administrativo — login/logout, criação/edição/
    desativação de usuário, troca de senha, cadastro estrutural (posto/entidade),
    api_key/retenção por posto, configuração do sistema. Ver banco.auditoria_registrar
    pelos pontos exatos que gravam aqui."""
    return banco.auditoria_listar(limit=limit, offset=offset, acao=acao, usuario_id=usuario_id)


@router.get("/recentes", dependencies=[Depends(deps.exigir_admin)])
def recentes():
    """Feed cru do estado em memória (todas as câmeras do processo) — mesmo motivo de
    `/ws` ficar admin-only: não é escopado por posto."""
    return estado.listar_recentes()


@router.get("/placa/{placa}")
def consultar_placa(placa: str, request: Request):
    """Retorna um JSON consolidado para a placa informada:
    última detecção, status na lista branca/negra e resumo do histórico.
    """
    placa = placa.upper().strip()
    escopo = deps.empresa_do_usuario(request)

    deteccoes = banco.listar_deteccoes(placa=placa, limit=50, empresa_id=escopo, placa_exata=True)
    # Total de verdade, não capado pelo `limit=50` acima (que existe só para não trazer
    # o histórico inteiro) — sem isto, uma placa com mais de 50 detecções sempre
    # aparecia com total "50", nunca o número real.
    total = banco.contar_deteccoes_placa(placa, empresa_id=escopo)

    lista_entry = banco.listas_buscar(placa, empresa_id=escopo)

    ultima = deteccoes[0] if deteccoes else None
    if ultima and ultima.get("bbox") and isinstance(ultima["bbox"], str):
        import json as _j
        try:
            ultima = dict(ultima)
            ultima["bbox"] = _j.loads(ultima["bbox"])
        except Exception:
            pass

    return {
        "placa":              placa,
        "padrao":             ultima["padrao"] if ultima else None,
        "lista":              lista_entry["tipo"] if lista_entry else None,
        "lista_descricao":    lista_entry["descricao"] if lista_entry else None,
        "total_deteccoes":    total,
        "ultima_deteccao":    ultima,
        "historico":          deteccoes[1:10],
    }


@router.get("/listas")
def listas_listar(request: Request, tipo: str | None = None,
                  limit: int = Query(1000, ge=1, le=5000)):
    """Listas branca/negra visíveis para quem pede.

    Escopada por posto: um `cliente` vê as entradas GLOBAIS (do sistema) mais as do próprio
    posto — nunca as de outro. Antes devolvia a tabela inteira a qualquer papel, com a
    descrição em texto livre junto (auditoria 27/08/2026, achado A3).
    """
    return banco.listas_listar(tipo=tipo, empresa_id=deps.empresa_do_usuario(request),
                               limit=limit)


@router.post("/listas", dependencies=[Depends(deps.exigir_admin)])
def listas_inserir(payload: dict, request: Request):
    placa = (payload.get("placa") or "").upper().strip()
    tipo = payload.get("tipo")
    descricao = payload.get("descricao", "")
    if not placa or tipo not in ("branca", "negra"):
        raise HTTPException(400, "placa e tipo (branca/negra) obrigatórios")
    # Admin de escopo cadastra para o próprio posto; admin global cadastra entrada GLOBAL
    # (empresa_id None), que é como todas as linhas antigas se comportam.
    try:
        id_ = banco.listas_inserir(placa, tipo, descricao,
                                   empresa_id=deps.empresa_do_usuario(request))
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"Placa {placa} já cadastrada")
    return {"id": id_}


@router.delete("/listas/{id_}", dependencies=[Depends(deps.exigir_admin)])
def listas_remover(id_: int, request: Request):
    # Escopo também na remoção: sem isto, um admin de posto apagaria entrada de outro só
    # sabendo o id (inteiro sequencial).
    escopo = deps.empresa_do_usuario(request)
    if escopo is not None:
        alvo = banco.listas_obter(id_)
        if alvo is None or alvo.get("empresa_id") != escopo:
            raise HTTPException(404, "Não encontrado")
    if not banco.listas_remover(id_):
        raise HTTPException(404, "Não encontrado")
    return {"removido": True}


@router.get("/status", dependencies=[Depends(deps.exigir_admin)])
def status():
    return estado.snapshot_status()


@router.get("/health", dependencies=[Depends(deps.exigir_admin)])
def health():
    """Status detalhado por câmera: liveness da thread, freshness do frame e histórico de restarts."""
    return sv.supervisor.health()


@router.get("/healthz")
def healthz():
    """Liveness para o healthcheck do container/orquestrador — público, sem dado nenhum.

    Precisa ser público: `/api/status` e `/api/health` exigem sessão, então um
    healthcheck apontado para eles recebe 401 (ou 303 para /login) e marca o container
    como unhealthy para sempre. Responde só que o processo está servindo HTTP; qualquer
    detalhe de câmera/posto continua atrás de autenticação em `/api/health`.
    """
    return {"status": "ok"}


# Chaves permitidas para configuração via interface (proteção contra payloads arbitrários).
CHAVES_CONFIG = set(config.PADROES.keys())

# Campos sensíveis — mascarados ao retornar (mas permitidos no POST).
#
# Usa a mesma convenção de `redacao.MASCARA`/`descartar_mascara` que `/api/cameras` e
# `/api/empresas` já usam, em vez de mascarar como "" (que era indistinguível de "nunca
# configurado" e tornava IMPOSSÍVEL limpar um destes 4 campos de propósito pela tela —
# a ambiguidade que motivou o endpoint paralelo `/api/apiplacas/uso`, abaixo). (Achado
# A7, review de 28/08/2026.)
CHAVES_SENSIVEIS = {"intelbras_senha", "smtp_senha", "api_key", "apiplacas_token"}


@router.get("/config", dependencies=[Depends(deps.exigir_admin)])
def config_obter():
    cfg = redacao.redigir(config.carregar(), tuple(CHAVES_SENSIVEIS))
    saida = {k: v for k, v in cfg.items() if k in CHAVES_CONFIG}
    saida["_padroes"] = config.PADROES
    return saida


@router.post("/camera/teste", dependencies=[Depends(deps.exigir_admin)])
def camera_teste(payload: dict):
    """Tenta abrir a câmera com os parâmetros recebidos e devolve um snapshot JPEG."""
    tipo = (payload.get("camera_tipo") or "usb").strip()
    indice = str(payload.get("camera_indice") or "0")
    try:
        largura = int(payload.get("camera_largura") or 1280)
        altura = int(payload.get("camera_altura") or 720)
        fps = int(payload.get("camera_fps") or 15)
    except (TypeError, ValueError):
        raise HTTPException(400, "camera_largura/altura/fps devem ser numéricos")

    intelbras = {
        "host": payload.get("intelbras_host", ""),
        "porta": payload.get("intelbras_porta", "554"),
        "usuario": payload.get("intelbras_usuario", "admin"),
        "senha": payload.get("intelbras_senha", "") or "",
        "canal": payload.get("intelbras_canal", "1"),
        "subtype": payload.get("intelbras_subtype", "1"),
        "formato":         payload.get("intelbras_formato", "padrao"),
        "rtsp_transporte": payload.get("rtsp_transporte", "tcp"),
    }
    # Se a senha vier vazia (UI mascara), usa a já salva no config
    if tipo in ("intelbras", "rtsp") and not intelbras["senha"]:
        intelbras["senha"] = config.carregar().get("intelbras_senha", "")

    ok, msg, jpg = camera_mod.capturar_teste(
        tipo=tipo, indice=indice, largura=largura, altura=altura, fps=fps, intelbras=intelbras
    )
    if not ok or jpg is None:
        raise HTTPException(503, msg)
    return Response(content=jpg, media_type="image/jpeg")


@router.get("/cameras")
def cameras_listar(request: Request, empresa_id: int | None = None):
    # `redacao.cameras` tira a senha do DVR: esta rota é aberta a `cliente` e `operador`
    # de propósito (é a tela de câmeras do posto), e o `SELECT *` trazia a credencial junto.
    return redacao.cameras(banco.cameras_listar(empresa_id=_empresa_efetiva(request, empresa_id)))


def _validar_camera(payload: dict) -> dict:
    """Valida nome/empresa da câmera. A câmera pertence a um posto (empresa) e o campo
    `local` diz onde ela está fisicamente instalada — sem o vínculo, num servidor central
    a lista de câmeras vira uma lista global sem dono.
    """
    # Campo que voltou mascarado = campo que a tela não mexeu: descarta para não gravar
    # "********" por cima do segredo real. Ver `redacao.descartar_mascara`.
    payload = redacao.descartar_mascara(payload)
    nome = (payload.get("nome") or "").strip()
    if not nome:
        raise HTTPException(400, "nome é obrigatório")
    empresa_id = payload.get("empresa_id")
    if not empresa_id:
        raise HTTPException(400, "empresa_id é obrigatório — toda câmera pertence a um posto")
    if not banco.empresas_obter(deps.inteiro_ou_400(empresa_id, 'empresa_id')):
        raise HTTPException(400, f"Empresa {empresa_id} não encontrada")
    return {**payload, "nome": nome, "local": (payload.get("local") or "").strip()}


@router.post("/cameras", dependencies=[Depends(deps.exigir_admin)])
def cameras_inserir(payload: dict):
    payload = _validar_camera(payload)
    try:
        id_ = banco.cameras_inserir(payload)
    except Exception as e:
        # A mensagem interna da exceção (texto do SQLite, caminho de arquivo) fica no
        # LOG, não na resposta ao cliente. (Auditoria 27/08/2026.)
        log.error("Falha em câmera: %s" % e, exc_info=True)
        raise HTTPException(500, "Operação falhou — veja o log do servidor.")
    # Inicia pipeline em background sem bloquear a resposta
    cam = banco.cameras_obter(id_)
    if cam and cam["ativo"]:
        cfg = config.carregar()
        threading.Thread(
            target=_iniciar_camera_bg, args=(id_, pipeline._cfg_para_camera(cfg, cam)),
            daemon=True, name=f"alpr-start-{id_}"
        ).start()
    return {"id": id_}


@router.put("/cameras/{id_}", dependencies=[Depends(deps.exigir_admin)])
def cameras_atualizar(id_: int, payload: dict):
    payload = _validar_camera(payload)
    try:
        ok = banco.cameras_atualizar(id_, payload)
    except Exception as e:
        # A mensagem interna da exceção (texto do SQLite, caminho de arquivo) fica no
        # LOG, não na resposta ao cliente. (Auditoria 27/08/2026.)
        log.error("Falha em câmera: %s" % e, exc_info=True)
        raise HTTPException(500, "Operação falhou — veja o log do servidor.")
    if not ok:
        raise HTTPException(404, "Câmera não encontrada")
    # empresa_id pode ter mudado — o HLS não pode continuar servindo pela permissão velha
    # até o cache expirar sozinho (não expira: só invalidação explícita, ver deps.py).
    deps.descartar_cache_camera(id_)
    # Reinicia o pipeline com a nova configuração. O cadastro JÁ foi gravado, mas o
    # pipeline pode continuar rodando a config antiga: `reiniciar_camera`/`parar_camera`
    # devolvem False quando a thread anterior não confirmou morte, e nesse caso elas se
    # recusam a abrir uma segunda conexão RTSP concorrente. Responder `{"atualizado":
    # True}` seco fazia a tela afirmar que a mudança estava no ar quando não estava; o
    # supervisor tenta de novo sozinho, e o campo abaixo é o que permite avisar quem
    # salvou que a config nova ainda não valeu.
    cam = banco.cameras_obter(id_)
    if cam and cam["ativo"]:
        cfg = config.carregar()
        pipeline_ok = pipeline.reiniciar_camera(id_, pipeline._cfg_para_camera(cfg, cam))
    else:
        pipeline_ok = pipeline.parar_camera(id_)
    return {"atualizado": True, "pipeline_aplicado": pipeline_ok}


@router.delete("/cameras/{id_}", dependencies=[Depends(deps.exigir_admin)])
def cameras_remover(id_: int):
    # bicos.camera_id é RESTRICT: a câmera não some enquanto algum bico depender dela.
    usos = banco.bicos_listar(camera_id=id_)
    if usos:
        codigos = ", ".join(b["codigo"] for b in usos[:5])
        raise HTTPException(
            409,
            f"Câmera em uso por {len(usos)} bico(s) ({codigos}) — remova ou realoque esses bicos antes.",
        )
    try:
        if not banco.cameras_remover(id_):
            raise HTTPException(404, "Câmera não encontrada")
    except sqlite3.IntegrityError:
        # A checagem acima e o DELETE não são atômicos — se um bico foi cadastrado
        # nessa câmera bem no meio da janela entre as duas, o RESTRICT dispara aqui.
        raise HTTPException(409, "Câmera passou a estar em uso por um bico durante a remoção — tente novamente.")
    deps.descartar_cache_camera(id_)
    # A linha já saiu do banco; o pipeline pode não ter parado. `parar_camera` devolve
    # False quando a thread não confirmou morte — e nesse caso ela NÃO desregistra a
    # instância nem fecha a câmera, de propósito (senão uma próxima chamada acharia a
    # câmera livre e abriria uma segunda conexão RTSP concorrente). Reportar
    # `{"removido": True}` seco escondia isso: o cadastro sumia da tela e a conexão
    # ficava presa. O supervisor volta a tentar liberar sozinho (ver
    # `_tentar_reiniciar`), então aqui basta ser honesto sobre o que ficou pendente.
    liberado = pipeline.parar_camera(id_)
    return {"removido": True, "pipeline_liberado": liberado}


@router.get("/cameras/{id_}/credenciais", dependencies=[Depends(deps.exigir_admin)])
def cameras_credenciais(id_: int, request: Request):
    """As credenciais REAIS da câmera, para o admin que vai editá-las.

    Existe porque `/api/cameras` e `/api/cameras/{id}/detalhe` passaram a redigir a senha
    do DVR (auditoria 27/08, K3) — e o formulário de edição precisa de algum caminho para
    preencher o campo. A diferença é que este tem gate de admin explícito e devolve SÓ os
    segredos, então nunca vira a rota conveniente que alguém usa por engano numa listagem.

    Continua valendo a checagem por posto: admin de escopo não abre credencial de outro.
    """
    cam = banco.cameras_obter(id_)
    if not cam:
        raise HTTPException(404, "Câmera não encontrada")
    deps.checar_acesso_empresa(request, cam.get("empresa_id"))
    quem_id, quem_nome = deps.quem_pede(request)
    banco.auditoria_registrar(
        usuario_id=quem_id, usuario_nome=quem_nome, acao="camera_credenciais_lidas",
        alvo_tipo="camera", alvo_id=id_, detalhe=f"nome={cam.get('nome', '?')}")
    return {campo: cam.get(campo, "") for campo in redacao.SEGREDOS_CAMERA}


@router.get("/cameras/{id_}/detalhe")
def cameras_detalhe(id_: int, request: Request):
    """Câmera + posto + bicos + estado da transmissão, para a página da câmera."""
    cam = banco.cameras_obter(id_)
    if not cam:
        raise HTTPException(404, "Câmera não encontrada")
    deps.checar_acesso_empresa(request, cam.get("empresa_id"))

    emp = banco.empresas_obter(cam["empresa_id"]) if cam.get("empresa_id") else None
    ent = banco.entidades_obter(emp["entidade_id"]) if emp else None
    # A partir daqui as duas linhas viram JSON: redige antes de qualquer uso na resposta.
    # `cam` cru já foi consumido acima para a checagem de acesso, que precisa do empresa_id.
    cam = redacao.camera(cam)
    emp = redacao.empresa(emp)

    automacoes = {a["id"]: a for a in banco.automacoes_listar()}
    bicos = []
    for b in banco.bicos_listar(camera_id=id_):
        # Um bico de 2 câmeras aparece nos editores das DUAS, e cada editor precisa gravar
        # no slot certo. Quem resolve isso é o servidor, não o JS: o retângulo está em
        # coordenadas do frame desta câmera, e deixar o cliente escolher o slot faria a
        # área ir para a câmera errada sem erro nenhum na hora.
        segunda = b.get("camera2_id")
        aqui_e_slot2 = bool(segunda) and segunda == id_
        outra_id = b["camera_id"] if aqui_e_slot2 else segunda
        outra = banco.cameras_obter(outra_id) if outra_id else None
        bicos.append({
            **b,
            "automacao_codigo": (automacoes.get(b["automacao_id"]) or {}).get("codigo", "?"),
            "slot": 2 if aqui_e_slot2 else 1,
            "roi_nesta_camera": b.get("roi2") if aqui_e_slot2 else b.get("roi"),
            "papel_nesta_camera": (b.get("papel_camera2") if aqui_e_slot2
                                   else b.get("papel_camera")) or "traseira",
            "outra_camera_id": outra_id,
            "outra_camera_nome": (outra.get("local") or outra.get("nome")) if outra else "",
            "outra_papel": (b.get("papel_camera") if aqui_e_slot2
                            else b.get("papel_camera2")) if outra_id else "",
            "outra_tem_roi": bool(b.get("roi") if aqui_e_slot2 else b.get("roi2")),
        })

    # "ao vivo" é ter IMAGEM saindo, não ter um objeto Pipeline registrado — ver
    # `pipeline.estado_stream`. A tela usava `id in _instancias` e, com
    # `deteccao_automatica=nao`, apontava o <img> para um MJPEG mudo.
    modo = pipeline.estado_stream(id_)
    ao_vivo = modo == "ao_vivo"
    # Nada de `a or b` aqui: com arrays numpy o `or` avalia o array inteiro como
    # booleano e levanta ValueError. Tem que ser comparação explícita com None.
    frame = estado.obter_frame_camera_limpo(id_)
    if frame is None:
        frame = estado.obter_frame_camera(id_)
    idade = None
    if estado.ultimo_frame_ts.get(id_):
        idade = round(time.time() - estado.ultimo_frame_ts[id_], 1)

    return {
        "camera": cam,
        "posto": emp,
        "entidade": ent,
        "bicos": bicos,
        "ao_vivo": ao_vivo,
        "stream_modo": modo,          # ao_vivo | aquecendo | sob_demanda
        "ultimo_frame_seg": idade,
        # A sobreposição das áreas precisa das dimensões reais do frame; o MJPEG nem
        # sempre expõe naturalWidth a tempo no navegador.
        "frame_largura": int(frame.shape[1]) if frame is not None else None,
        "frame_altura": int(frame.shape[0]) if frame is not None else None,
    }


@router.get("/cameras/{id_}/snapshot")
def cameras_snapshot(id_: int, request: Request):
    """Frame atual da câmera como JPEG — usado pelo editor de área de captura.

    No modo reativo não há pipeline contínuo alimentando `estado`, então cai para uma
    captura direta (conecta, pega 1 frame, desconecta). Sem esse fallback o editor de ROI
    fica inutilizável justamente na configuração que o servidor central usa.
    """
    import cv2

    # Uma consulta só, e o 404 ANTES de qualquer leitura de `estado`. O `if cam_check:`
    # anterior deixava a checagem de acesso ser PULADA quando a linha não existia — e esse
    # estado é alcançável: quando `Pipeline.parar()` não confirma, a thread zumbi segue
    # publicando em `estado.frames_cameras` e o `pop` nunca roda, enquanto
    # `DELETE /api/cameras/{id}` já respondeu `{"removido": true, "pipeline_liberado": false}`
    # com a linha apagada. Resultado: apagada a câmera 9 do posto 7, QUALQUER usuário logado
    # recebia o JPEG ao vivo daquele pátio. (Auditoria 27/08/2026, achado K4.)
    cam = banco.cameras_obter(id_)
    if not cam:
        raise HTTPException(404, "Câmera não encontrada")
    deps.checar_acesso_empresa(request, cam.get("empresa_id"))

    frame = estado.obter_frame_camera(id_)
    if frame is None:
        cfg = config.carregar()
        with leitura.lock_camera(id_):     # respeita o limite de 1 conexão RTSP por câmera
            frame = camera_mod.capturar_frame_unico(
                tipo=cam["camera_tipo"],
                indice=cam.get("rtsp_url_custom") or cam.get("camera_indice", "0"),
                largura=int(cfg.get("camera_largura", "1280")),
                altura=int(cfg.get("camera_altura", "720")),
                fps=int(cfg.get("camera_fps", "15")),
                intelbras={
                    "host": "" if cam.get("rtsp_url_custom") else cam.get("intelbras_host", ""),
                    "porta": cam.get("intelbras_porta", "554"),
                    "usuario": cam.get("intelbras_usuario", "admin"),
                    "senha": cam.get("intelbras_senha") or cfg.get("intelbras_senha", ""),
                    "canal": cam.get("intelbras_canal", "1"),
                    "subtype": cam.get("intelbras_subtype", "1"),
                    "formato": cam.get("intelbras_formato", "padrao"),
                    "rtsp_transporte": cfg.get("rtsp_transporte", "tcp"),
                },
            )
        if frame is None:
            raise HTTPException(503, "Não foi possível capturar imagem da câmera — verifique a conexão")
    ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok:
        raise HTTPException(500, "Falha ao codificar frame")
    return Response(content=jpg.tobytes(), media_type="image/jpeg")


@router.post("/cameras/{id_}/teste")
def cameras_teste(id_: int, request: Request):
    import cv2

    cam = banco.cameras_obter(id_)
    if not cam:
        raise HTTPException(404, "Câmera não encontrada")
    deps.checar_acesso_empresa(request, cam.get("empresa_id"))

    # Se há pipeline rodando para esta câmera, aguarda frame (evita segunda conexão RTSP)
    import time as _time
    if id_ in pipeline._instancias:
        for _ in range(80):          # até 8s esperando o primeiro frame
            frame = estado.obter_frame_camera(id_)
            if frame is not None:
                break
            _time.sleep(0.1)
        if frame is not None:
            ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            if ok:
                return Response(content=jpg.tobytes(), media_type="image/jpeg")
        raise HTTPException(503, "Pipeline iniciado mas câmera ainda sem frame — aguarde e tente novamente")

    # Câmera ainda não está no pipeline — tenta conexão direta
    intelbras = {
        "host": cam["intelbras_host"],
        "porta": cam["intelbras_porta"],
        "usuario": cam["intelbras_usuario"],
        "senha": cam["intelbras_senha"] or config.carregar().get("intelbras_senha", ""),
        "canal": cam["intelbras_canal"],
        "subtype": cam["intelbras_subtype"],
        "formato": cam["intelbras_formato"],
    }
    ok, msg, jpg = camera_mod.capturar_teste(
        tipo=cam["camera_tipo"],
        indice=cam["camera_indice"],
        largura=1280, altura=720, fps=15,
        intelbras=intelbras,
    )
    if not ok or jpg is None:
        raise HTTPException(503, msg)
    return Response(content=jpg, media_type="image/jpeg")


@router.get("/cameras/rede-local", dependencies=[Depends(deps.exigir_admin)])
def cameras_rede_local():
    """Retorna a sub-rede local do servidor para sugerir no scan."""
    import ipaddress
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip_local = s.getsockname()[0]
        s.close()
        net = ipaddress.ip_network(f"{ip_local}/24", strict=False)
        return {"ip_servidor": ip_local, "rede_sugerida": str(net)}
    except Exception:
        return {"ip_servidor": None, "rede_sugerida": "192.168.1.0/24"}


@router.post("/cameras/scan", dependencies=[Depends(deps.exigir_admin)])
def cameras_scan(payload: dict):
    """Varre uma faixa de IPs em busca de hosts com porta RTSP aberta."""
    import ipaddress
    import socket as _socket
    from concurrent.futures import ThreadPoolExecutor, as_completed

    rede = (payload.get("rede") or "").strip()
    porta = int(payload.get("porta") or 554)
    timeout = min(float(payload.get("timeout") or 0.3), 2.0)

    if not rede:
        raise HTTPException(400, "Campo 'rede' é obrigatório (ex: 192.168.1.0/24)")
    try:
        net = ipaddress.ip_network(rede, strict=False)
    except ValueError:
        raise HTTPException(400, f"Rede inválida: '{rede}'. Use CIDR (ex: 192.168.1.0/24)")

    if net.num_addresses > 1024:
        raise HTTPException(400, "Rede muito grande — limite: /22 (1024 endereços)")

    hosts = [str(ip) for ip in net.hosts()]

    def _check(ip: str):
        try:
            with _socket.create_connection((ip, porta), timeout=timeout):
                return ip
        except Exception:
            return None

    encontrados = []
    with ThreadPoolExecutor(max_workers=min(128, len(hosts))) as ex:
        for resultado in as_completed({ex.submit(_check, ip): ip for ip in hosts}):
            ip = resultado.result()
            if ip:
                encontrados.append(ip)

    return {
        "hosts": sorted(encontrados, key=lambda x: tuple(int(p) for p in x.split("."))),
        "total": len(encontrados),
        "rede": rede,
        "porta": porta,
    }


@router.get("/modelos", dependencies=[Depends(deps.exigir_admin)])
def modelos_listar():
    """Lista arquivos .onnx disponíveis na pasta models/."""
    from pathlib import Path
    pasta = Path("models")
    if not pasta.exists():
        return []
    return sorted(f.name for f in pasta.glob("*.onnx"))


@router.get("/debug/ocr_crop", dependencies=[Depends(deps.exigir_admin)])
def debug_ocr_crop():
    """Retorna o último crop enviado ao Tesseract como JPEG (para debug visual)."""
    import cv2
    import numpy as np
    jpg = estado.ultimo_crop_ocr_jpg
    if jpg is None:
        # Placeholder cinza enquanto não há detecção
        ph = np.full((60, 240), 60, dtype=np.uint8)
        cv2.putText(ph, "aguardando...", (8, 38), cv2.FONT_HERSHEY_SIMPLEX, 0.6, 200, 1)
        _, buf = cv2.imencode(".jpg", ph)
        jpg = buf.tobytes()
    return Response(content=jpg, media_type="image/jpeg")


@router.get("/bicos/{bico_id}/preview.jpg")
def bicos_preview(bico_id: int, request: Request, camera_id: int | None = None):
    """Última foto de preview (recorte + caixa) da leitura mais recente deste bico —
    o que a tela do posto e o editor de ROI mostram como "foi isto que a câmera viu".

    Sobrescrita a cada `/api/leitura`/teste manual; não é registro histórico (esse é
    `snapshot`/`frame` em `deteccoes`, servido por `/static/snapshots`). Fica atrás de
    autenticação porque `bico_id` é um inteiro sequencial pequeno COMPARTILHADO entre
    todos os clientes — sem este gate, bastava iterar `/static/snapshots/preview_bico_1.
    jpg`, `_2.jpg`... para ver a placa mais recente de qualquer posto sem login (por isso
    o arquivo não mora mais dentro de `app/web/static/` — ver `app/visao/leitura.py:
    PREVIEW_DIR`).

    Sem `camera_id`: o quadro de onde saiu a placa eleita (o que o roteador sempre
    recebeu). Com `camera_id`: o quadro daquela câmera do bico, para conferir as duas
    quando o bico tem duas.
    """
    bico = banco.bicos_obter(bico_id)
    if not bico:
        raise HTTPException(404, "Não encontrado")
    automacao = banco.automacoes_obter(bico["automacao_id"])
    deps.checar_acesso_empresa(request, automacao["empresa_id"] if automacao else None)

    if camera_id is not None and camera_id not in (bico["camera_id"], bico.get("camera2_id")):
        # Sem esta checagem o parâmetro viraria um seletor de caminho de arquivo dentro de
        # `dados_privados/` — a mesma classe de falha que motivou tirar o preview de
        # `static/`: qualquer sessão autenticada leria o preview de qualquer bico.
        raise HTTPException(404, "Câmera não é deste bico")
    caminho = leitura.caminho_preview_bico(bico_id, camera_id)
    if not caminho.exists():
        raise HTTPException(404, "Sem preview para este bico ainda")
    # no-store: o arquivo é sobrescrito a cada leitura, e o mesmo nome nunca deve ser
    # servido do cache (do navegador ou de um proxy) depois que o conteúdo já mudou.
    return FileResponse(caminho, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


@router.post("/config", dependencies=[Depends(deps.exigir_admin)])
def config_salvar(payload: dict, request: Request):
    if not isinstance(payload, dict):
        raise HTTPException(400, "payload inválido")

    atual = config.carregar()
    invalidas = [k for k in payload if k not in CHAVES_CONFIG]
    if invalidas:
        raise HTTPException(400, f"chaves desconhecidas: {invalidas}")

    # Campo sensível que voltou MASCARADO = a tela não mexeu — descarta antes de mesclar,
    # para não gravar "********" por cima do segredo real (mesmo mecanismo de
    # /api/cameras). "" deixa de significar "preservar" e passa a significar "limpar de
    # propósito", igual a rtsp_url_custom em /api/cameras — antes desta mudança era
    # IMPOSSÍVEL limpar um destes 4 campos pela tela. (Achado A7.)
    payload = redacao.descartar_mascara(payload, tuple(CHAVES_SENSIVEIS))

    novo = dict(atual)
    for k, v in payload.items():
        novo[k] = str(v) if v is not None else ""

    # Filtra só as chaves conhecidas (descarta lixo herdado, ex.: _padroes).
    novo = {k: novo[k] for k in CHAVES_CONFIG if k in novo}
    # Só os NOMES das chaves que mudaram vão pra auditoria — nunca o valor: várias
    # (intelbras_senha, api_key, smtp_senha) são segredo, e mesmo as que não são não
    # precisam virar histórico permanente só por terem passado por aqui.
    mudadas = sorted(k for k in novo if atual.get(k) != novo.get(k))
    config.salvar(novo)
    log.info("Configuração salva via interface")
    quem_id, quem_nome = deps.quem_pede(request)
    banco.auditoria_registrar(usuario_id=quem_id, usuario_nome=quem_nome, acao="config_salva",
                              alvo_tipo="config", detalhe=", ".join(mudadas) or "sem mudanças")

    reiniciado = False
    try:
        pipeline.reiniciar(novo)
        reiniciado = True
        log.info("Pipeline reiniciado com nova configuração")
    except Exception as e:
        log.error("Falha ao reiniciar pipeline com nova config: %s", e)

    sv.supervisor.atualizar_cfg(novo)

    # Coletor de dataset: sem isto, LIGAR `captura_dataset` pela tela era no-op até
    # reiniciar o processo — justamente no modo `deteccao_automatica=nao`, onde ele é o
    # ÚNICO mecanismo de coleta. Desligar já funcionava (o laço relê a config a cada volta),
    # e essa assimetria tornava o bug ainda mais confuso. `iniciar_coletor` é idempotente:
    # ele para o que estiver rodando antes de subir, então salvar dez vezes não empilha dez
    # coletores por câmera. (Auditoria 27/08/2026, achado A10.)
    #
    # EM THREAD: `iniciar_coletor` chama `parar_coletor()` primeiro, que faz `join`
    # SEQUENCIAL de uma thread por câmera (até 5s cada) — síncrono aqui, o pior caso
    # (N câmeras × 5s) travava a resposta HTTP desta rota. Mesmo padrão de
    # `_iniciar_camera_bg`, que já sobe o pipeline em background por este motivo.
    # (Achado A6, review de 28/08/2026.)
    threading.Thread(target=_iniciar_coletor_bg, args=(novo,), daemon=True,
                     name="alpr-coletor-cfg").start()

    # Mesma classe de problema, mesmo remédio: `streaming_modo` também só era lido no boot,
    # então trocar MJPEG↔HLS no painel não fazia nada até reiniciar.
    if "streaming_modo" in mudadas:
        try:
            from app.streaming import hls_encoder as _hls
            _hls.hls_manager.parar()
            if novo.get("streaming_modo", "mjpeg") == "hls":
                if not _hls.hls_manager.iniciar(banco.cameras_listar()):
                    log.warning("HLS não subiu — a tela cai para MJPEG")
        except Exception as e:
            log.error("Falha ao aplicar streaming_modo: %s", e)

    # Trocar o token tem de liberar o disjuntor da apiplacas na hora. Ele pausa por 15min
    # (x4 no caso de token inválido) justamente para não insistir no que não pode dar
    # certo — mas quem acabou de corrigir o token no painel esperaria efeito imediato, e
    # sem isto concluiria, com razão, que a tela não funciona.
    if "apiplacas_token" in mudadas:
        try:
            from app.integracoes import apiplacas
            apiplacas.limpar_pausa()
        except Exception as e:
            log.warning("Falha ao liberar a pausa da apiplacas: %s", e)

    return {"salvo": True, "pipeline_reiniciado": reiniciado}


@router.get("/apiplacas/saldo", dependencies=[Depends(deps.exigir_admin)])
def apiplacas_saldo():
    """Crédito restante na apiplacas.

    Existe para que "o crédito acabou" seja percebido ANTES de virar sintoma. Sem isto o
    problema chega como "o combustível parou de vir no payload", que é um diagnóstico
    caro. Nunca é chamada do caminho da leitura, e tem freio próprio no módulo (6/hora):
    é uma chamada externa atrás de um botão de painel, e botão de painel é clicado em
    sequência.
    """
    cfg = config.carregar()
    if not apiplacas.configurado(cfg):
        return {"qtd_consultas": None, "erro": "consulta de veículo não configurada"}
    qtd = apiplacas.saldo(cfg)
    return {"qtd_consultas": qtd,
            "erro": "" if qtd is not None else "não foi possível consultar o saldo"}


@router.get("/apiplacas/uso", dependencies=[Depends(deps.exigir_admin)])
def apiplacas_uso():
    """Quanto o cache guarda e quanto já se pagou — sem depender do provedor.

    `consultas` conta chamadas PAGAS; `total` conta placas distintas em cache. A diferença
    entre os dois é exatamente o que o cache economizou.
    """
    cfg = config.carregar()
    st = banco.veiculos_stats()
    try:
        custo = float(cfg.get("apiplacas_custo_consulta") or 0)
    except ValueError:
        custo = 0.0
    return {**st, "custo_consulta": custo,
            "gasto_estimado": round(st["consultas"] * custo, 2),
            # `GET /api/config` mascara o token, e mascara IGUAL nos dois casos: devolve
            # "" tanto para "salvo" quanto para "nunca preenchido". Sem este booleano a
            # tela não tem como dizer se o recurso está de pé — e o modo de falha desta
            # feature é silencioso (o combustível simplesmente não vem), então "parece
            # configurado mas não está" passaria despercebido por semanas.
            "ativo": config.get_bool(cfg, "apiplacas_ativo"),
            "modo": (cfg.get("apiplacas_modo") or "manual").strip().lower(),
            "token_configurado": bool((cfg.get("apiplacas_token") or "").strip())}


# ─── Consulta de veículo sob demanda ───────────────────────────────────────
# Com cota curta, gastar é decisão consciente: o abastecimento não consulta sozinho
# (`apiplacas_modo=manual`) e estas rotas são o caminho deliberado. Só UMA delas gasta,
# e é a única do painel inteiro que gasta.

@router.get("/veiculos")
def veiculos_em_cache(request: Request, placas: str = ""):
    """Dados JÁ EM CACHE das placas informadas. NUNCA consulta a API. `{placa: bloco|null}`.

    É o que pinta a coluna "Combustível" do histórico: uma requisição por página em vez de
    uma por linha. Cache-only sem opção de gastar de propósito — esta rota é chamada em
    toda navegação do histórico, e um gasto acidental aqui seria proporcional ao uso do
    painel, que é exatamente o que não pode acontecer.
    """
    pedidas = [p.strip().upper() for p in placas.split(",") if p.strip()][:500]
    if not pedidas:
        return {}
    cfg = config.carregar()
    if not config.get_bool(cfg, "apiplacas_ativo"):
        return {}
    saida: dict[str, dict | None] = {}
    for placa in pedidas:
        try:
            bloco = apiplacas.consultar(placa, cfg, permitir_gasto=False)
        except Exception as e:
            log.warning("Falha ao ler cache de veículo de %s: %s", placa, e)
            bloco = None
        # Só devolve o que EXISTE no cache: `indisponivel` aqui significa "não consultado
        # ainda", e a tela precisa distinguir isso de "consultado e sem dados" para saber
        # se oferece o botão de consultar.
        saida[placa] = bloco if bloco and bloco["consulta"] != apiplacas.CONSULTA_INDISPONIVEL else None
    return saida


@router.get("/veiculos/pendentes", dependencies=[Depends(deps.exigir_admin)])
def veiculos_pendentes(request: Request, limit: int = Query(20, ge=1, le=200)):
    """Placas mais vistas que ainda não têm dados, com o custo estimado de consultá-las.

    É o que a consulta em lote propõe. Devolve o custo junto para a confirmação poder
    mostrar quanto vai gastar ANTES de gastar — pedir confirmação sem dizer o preço não é
    confirmação.
    """
    cfg = config.carregar()
    pendentes = banco.veiculos_pendentes(limit=limit, empresa_id=deps.empresa_do_usuario(request))
    try:
        custo = float(cfg.get("apiplacas_custo_consulta") or 0)
    except ValueError:
        custo = 0.0
    return {"placas": pendentes, "custo_consulta": custo,
            "custo_total": round(len(pendentes) * custo, 2)}


@router.post("/veiculos/{placa}/consultar", dependencies=[Depends(deps.exigir_admin)])
def veiculo_consultar(placa: str, request: Request):
    """Consulta PAGA de uma placa, disparada por um humano. **Gasta 1 crédito.**

    É a única rota do painel que gasta, e gasta independentemente de `apiplacas_modo`:
    manual é manual, e o modo governa apenas quem consulta SOZINHO.

    Admin, e não operador: ver o combustível é operar, gastar crédito é administrar — a
    mesma divisão que o projeto já faz entre usar o sistema e configurá-lo. Auditada pelo
    mesmo motivo: ação que custa dinheiro precisa ter dono.

    Todos os freios do módulo continuam valendo (disjuntor, tetos, cooldown por placa). O
    cooldown fica de propósito: é ele que impede um clique duplo de virar dois créditos.
    """
    cfg = config.carregar()
    if not apiplacas.configurado(cfg):
        raise HTTPException(400, "Consulta de veículo não configurada — falta o token em Configuração.")

    placa_norm = apiplacas.normalizar_placa(placa)
    if not apiplacas.placa_consultavel(placa_norm):
        raise HTTPException(400, f"Placa '{placa}' fora dos formatos AAA0000/AAA0A00.")

    ja_tinha = banco.veiculos_obter(placa_norm) is not None
    bloco = apiplacas.consultar(placa_norm, cfg, permitir_gasto=True)

    # Audita só quando houve chamada externa de verdade: um clique que caiu no cache não
    # gastou nada e não precisa virar linha permanente de auditoria.
    if bloco["origem"] == "api":
        quem_id, quem_nome = deps.quem_pede(request)
        banco.auditoria_registrar(
            usuario_id=quem_id, usuario_nome=quem_nome, acao="veiculo_consultado",
            alvo_tipo="placa", alvo_id=placa_norm,
            detalhe=f"consulta paga ({bloco['consulta']})" + ("" if ja_tinha else " — placa nova"),
        )
    return {"placa": placa_norm, "veiculo": bloco}


@router.post("/setup/concluir", dependencies=[Depends(deps.exigir_admin)])
def setup_concluir(payload: dict):
    """Grava configurações do wizard e marca sistema como implantado."""
    atual = config.carregar()
    permitidos = {"porta", "ocr_engine", "deteccao_automatica", "log_level",
                  "webhook_url", "webhook_todas", "alerta_lista_negra"}
    for k, v in payload.items():
        if k in permitidos and v is not None:
            atual[k] = str(v)
    atual["implantado"] = "sim"
    config.salvar(atual)
    log.info("Implantação concluída via wizard de primeiro uso")
    return {"ok": True}
