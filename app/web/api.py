"""API REST para detecções, listas e status."""
from __future__ import annotations
import logging
import sqlite3
import threading
import time

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app.core import banco
from app.core import config
from app.core import estado
from app.operacao import supervisor as sv
from app.visao import camera as camera_mod
from app.visao import leitura
from app.visao import pipeline

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def _iniciar_camera_bg(camera_db_id: int, cam_cfg: dict) -> None:
    try:
        pipeline.iniciar_camera(camera_db_id, cam_cfg)
    except Exception as e:
        log.error("Falha ao iniciar câmera %d: %s", camera_db_id, e)


@router.get("/deteccoes")
def listar_deteccoes(
    placa: str | None = None,
    desde: str | None = None,
    ate: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    empresa_id: int | None = None,
    bico_id: int | None = None,
    incluir_testes: bool = False,
):
    return banco.listar_deteccoes(placa=placa, desde=desde, ate=ate, limit=limit,
                                  offset=offset, empresa_id=empresa_id, bico_id=bico_id,
                                  incluir_testes=incluir_testes)


@router.get("/chamadas")
def chamadas_listar(
    limit: int = Query(50, ge=1, le=500),
    empresa_id: int | None = None,
    status: str | None = None,
    apenas_erros: bool = False,
):
    """Chamadas do roteador ao endpoint reativo — inclusive as recusadas."""
    return banco.chamadas_listar(limit=limit, empresa_id=empresa_id,
                                 status=status, apenas_erros=apenas_erros)


@router.get("/chamadas/resumo")
def chamadas_resumo(horas: int = Query(24, ge=1, le=720)):
    return banco.chamadas_resumo(horas=horas)


@router.delete("/deteccoes/{id_}")
def remover_deteccao(id_: int):
    if not banco.remover_deteccao(id_):
        raise HTTPException(404, "Detecção não encontrada")
    return {"removido": True}


@router.get("/stats")
def stats():
    cfg = config.carregar()
    return {
        **banco.stats(),
        "fps": estado.fps_atual,
        "uptime_seg": estado.uptime_segundos(),
        "pipeline": estado.pipeline_rodando,
        "deteccao_automatica": cfg.get("deteccao_automatica", "sim").lower() in ("sim", "true", "1"),
        "streaming_modo": cfg.get("streaming_modo", "mjpeg"),
    }


@router.get("/logs")
def logs(nivel: str | None = None, limit: int = Query(100, ge=1, le=200)):
    todos = estado.listar_logs()
    if nivel:
        nivel = nivel.upper()
        todos = [l for l in todos if l["level"] == nivel]
    return todos[:limit]


@router.delete("/logs")
def limpar_logs():
    estado.limpar_logs()
    return {"limpo": True}


@router.get("/recentes")
def recentes():
    return estado.listar_recentes()


@router.get("/placa/{placa}")
def consultar_placa(placa: str):
    """Retorna um JSON consolidado para a placa informada:
    última detecção, status na lista branca/negra e resumo do histórico.
    """
    placa = placa.upper().strip()

    deteccoes = banco.listar_deteccoes(placa=placa, limit=50)
    # filtra exato (listar_deteccoes usa LIKE %placa%)
    deteccoes = [d for d in deteccoes if d["placa"] == placa]

    lista_entry = banco.listas_buscar(placa)

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
        "total_deteccoes":    len(deteccoes),
        "ultima_deteccao":    ultima,
        "historico":          deteccoes[1:10],
    }


@router.get("/listas")
def listas_listar(tipo: str | None = None):
    return banco.listas_listar(tipo=tipo)


@router.post("/listas")
def listas_inserir(payload: dict):
    placa = (payload.get("placa") or "").upper().strip()
    tipo = payload.get("tipo")
    descricao = payload.get("descricao", "")
    if not placa or tipo not in ("branca", "negra"):
        raise HTTPException(400, "placa e tipo (branca/negra) obrigatórios")
    try:
        id_ = banco.listas_inserir(placa, tipo, descricao)
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"Placa {placa} já cadastrada")
    return {"id": id_}


@router.delete("/listas/{id_}")
def listas_remover(id_: int):
    if not banco.listas_remover(id_):
        raise HTTPException(404, "Não encontrado")
    return {"removido": True}


@router.get("/status")
def status():
    return estado.snapshot_status()


@router.get("/health")
def health():
    """Status detalhado por câmera: liveness da thread, freshness do frame e histórico de restarts."""
    return sv.supervisor.health()


# Chaves permitidas para configuração via interface (proteção contra payloads arbitrários).
CHAVES_CONFIG = set(config.PADROES.keys())

# Campos sensíveis — mascarados ao retornar (mas permitidos no POST).
CHAVES_SENSIVEIS = {"intelbras_senha"}


@router.get("/config")
def config_obter():
    cfg = config.carregar()
    saida = {}
    for k, v in cfg.items():
        if k not in CHAVES_CONFIG:
            continue
        saida[k] = "" if k in CHAVES_SENSIVEIS and v else v
    saida["_padroes"] = config.PADROES
    return saida


@router.post("/camera/teste")
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
def cameras_listar(empresa_id: int | None = None):
    return banco.cameras_listar(empresa_id=empresa_id)


def _validar_camera(payload: dict) -> dict:
    """Valida nome/empresa da câmera. A câmera pertence a um posto (empresa) e o campo
    `local` diz onde ela está fisicamente instalada — sem o vínculo, num servidor central
    a lista de câmeras vira uma lista global sem dono.
    """
    nome = (payload.get("nome") or "").strip()
    if not nome:
        raise HTTPException(400, "nome é obrigatório")
    empresa_id = payload.get("empresa_id")
    if not empresa_id:
        raise HTTPException(400, "empresa_id é obrigatório — toda câmera pertence a um posto")
    if not banco.empresas_obter(int(empresa_id)):
        raise HTTPException(400, f"Empresa {empresa_id} não encontrada")
    return {**payload, "nome": nome, "local": (payload.get("local") or "").strip()}


@router.post("/cameras")
def cameras_inserir(payload: dict):
    payload = _validar_camera(payload)
    try:
        id_ = banco.cameras_inserir(payload)
    except Exception as e:
        raise HTTPException(500, str(e))
    # Inicia pipeline em background sem bloquear a resposta
    cam = banco.cameras_obter(id_)
    if cam and cam["ativo"]:
        cfg = config.carregar()
        threading.Thread(
            target=_iniciar_camera_bg, args=(id_, pipeline._cfg_para_camera(cfg, cam)),
            daemon=True, name=f"alpr-start-{id_}"
        ).start()
    return {"id": id_}


@router.put("/cameras/{id_}")
def cameras_atualizar(id_: int, payload: dict):
    payload = _validar_camera(payload)
    try:
        ok = banco.cameras_atualizar(id_, payload)
    except Exception as e:
        raise HTTPException(500, str(e))
    if not ok:
        raise HTTPException(404, "Câmera não encontrada")
    # Reinicia o pipeline com a nova configuração
    cam = banco.cameras_obter(id_)
    if cam and cam["ativo"]:
        cfg = config.carregar()
        pipeline.reiniciar_camera(id_, pipeline._cfg_para_camera(cfg, cam))
    else:
        pipeline.parar_camera(id_)
    return {"atualizado": True}


@router.delete("/cameras/{id_}")
def cameras_remover(id_: int):
    # bicos.camera_id é RESTRICT: a câmera não some enquanto algum bico depender dela.
    usos = banco.bicos_listar(camera_id=id_)
    if usos:
        codigos = ", ".join(b["codigo"] for b in usos[:5])
        raise HTTPException(
            409,
            f"Câmera em uso por {len(usos)} bico(s) ({codigos}) — remova ou realoque esses bicos antes.",
        )
    if not banco.cameras_remover(id_):
        raise HTTPException(404, "Câmera não encontrada")
    pipeline.parar_camera(id_)
    return {"removido": True}


@router.get("/cameras/{id_}/detalhe")
def cameras_detalhe(id_: int):
    """Câmera + posto + bicos + estado da transmissão, para a página da câmera."""
    cam = banco.cameras_obter(id_)
    if not cam:
        raise HTTPException(404, "Câmera não encontrada")

    emp = banco.empresas_obter(cam["empresa_id"]) if cam.get("empresa_id") else None
    ent = banco.entidades_obter(emp["entidade_id"]) if emp else None

    automacoes = {a["id"]: a for a in banco.automacoes_listar()}
    bicos = [{**b, "automacao_codigo": (automacoes.get(b["automacao_id"]) or {}).get("codigo", "?")}
             for b in banco.bicos_listar(camera_id=id_)]

    ao_vivo = id_ in pipeline._instancias
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
        "ultimo_frame_seg": idade,
        # A sobreposição das áreas precisa das dimensões reais do frame; o MJPEG nem
        # sempre expõe naturalWidth a tempo no navegador.
        "frame_largura": int(frame.shape[1]) if frame is not None else None,
        "frame_altura": int(frame.shape[0]) if frame is not None else None,
    }


@router.get("/cameras/{id_}/snapshot")
def cameras_snapshot(id_: int):
    """Frame atual da câmera como JPEG — usado pelo editor de área de captura.

    No modo reativo não há pipeline contínuo alimentando `estado`, então cai para uma
    captura direta (conecta, pega 1 frame, desconecta). Sem esse fallback o editor de ROI
    fica inutilizável justamente na configuração que o servidor central usa.
    """
    import cv2

    frame = estado.obter_frame_camera(id_)
    if frame is None:
        cam = banco.cameras_obter(id_)
        if not cam:
            raise HTTPException(404, "Câmera não encontrada")
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
def cameras_teste(id_: int):
    import cv2

    cam = banco.cameras_obter(id_)
    if not cam:
        raise HTTPException(404, "Câmera não encontrada")

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


@router.get("/cameras/rede-local")
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


@router.post("/cameras/scan")
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


@router.get("/modelos")
def modelos_listar():
    """Lista arquivos .onnx disponíveis na pasta models/."""
    from pathlib import Path
    pasta = Path("models")
    if not pasta.exists():
        return []
    return sorted(f.name for f in pasta.glob("*.onnx"))


@router.get("/debug/ocr_crop")
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


@router.post("/config")
def config_salvar(payload: dict):
    if not isinstance(payload, dict):
        raise HTTPException(400, "payload inválido")

    atual = config.carregar()
    invalidas = [k for k in payload if k not in CHAVES_CONFIG]
    if invalidas:
        raise HTTPException(400, f"chaves desconhecidas: {invalidas}")

    novo = dict(atual)
    for k, v in payload.items():
        if k in CHAVES_SENSIVEIS and (v is None or v == ""):
            continue
        novo[k] = str(v) if v is not None else ""

    # Filtra só as chaves conhecidas (descarta lixo herdado, ex.: _padroes).
    novo = {k: novo[k] for k in CHAVES_CONFIG if k in novo}
    config.salvar(novo)
    log.info("Configuração salva via interface")

    reiniciado = False
    try:
        pipeline.reiniciar(novo)
        reiniciado = True
        log.info("Pipeline reiniciado com nova configuração")
    except Exception as e:
        log.error("Falha ao reiniciar pipeline com nova config: %s", e)

    sv.supervisor.atualizar_cfg(novo)
    return {"salvo": True, "pipeline_reiniciado": reiniciado}


@router.post("/setup/concluir")
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
