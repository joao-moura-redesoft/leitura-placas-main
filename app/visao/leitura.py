"""Lógica de domínio da leitura de placa sob demanda ("Ler Placa" / GET reativo).

Reusado por dois endpoints: o reativo multi-tenant (GET /api/leitura, por
entidade/cnpj/automacao/bico) e o teste manual por bico (POST /api/bicos/{id}/
ler-placa-teste, usado pela tela do posto e pelo editor de áreas) — mesmo loop
reject-retry nos dois casos, só muda como a câmera/ROI são resolvidas antes de
chamar ler_placa().

Não importa nada de app/web/ (regra de dependência do projeto: visao importa só core).
Levanta LeituraError em vez de HTTPException — cada rota HTTP converte pro código que
fizer sentido no seu contexto.
"""
from __future__ import annotations
import logging
import re
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from app.core import banco
from app.core import estado
from app.visao import contexto_log
from app.visao.camera import Camera
# Alias com underscore: `confirmada` é o nome da variável local em `ler_placa`.
from app.visao.consenso import confirmada as _confirmada
from app.visao.pipeline import _expandir_bbox
from app.visao.validador import parecidas, validar

log = logging.getLogger(__name__)

SNAPSHOT_DIR = Path("app/web/static/snapshots")


@dataclass
class EspecificacaoCamera:
    camera_tipo: str
    camera_indice: str
    intelbras_host: str
    intelbras_porta: str
    intelbras_usuario: str
    intelbras_senha: str
    intelbras_canal: str
    intelbras_subtype: str
    intelbras_formato: str
    rtsp_url_custom: str = ""

    @classmethod
    def de_camera_db(cls, cam: dict, cfg: dict) -> "EspecificacaoCamera":
        return cls(
            camera_tipo=cam["camera_tipo"],
            camera_indice=cam.get("camera_indice", "0"),
            intelbras_host=cam.get("intelbras_host", ""),
            intelbras_porta=cam.get("intelbras_porta", "554"),
            intelbras_usuario=cam.get("intelbras_usuario", "admin"),
            intelbras_senha=cam.get("intelbras_senha") or cfg.get("intelbras_senha", ""),
            intelbras_canal=cam.get("intelbras_canal", "1"),
            intelbras_subtype=cam.get("intelbras_subtype", "1"),
            intelbras_formato=cam.get("intelbras_formato", "padrao"),
            rtsp_url_custom=cam.get("rtsp_url_custom", ""),
        )


class LeituraError(Exception):
    def __init__(self, status: int, mensagem: str):
        super().__init__(mensagem)
        self.status = status
        self.mensagem = mensagem


# Posições esperadas por formato (L=letra, D=dígito). Mercosul: pos-5 é LETRA.
_PADRAO_POS = {"mercosul": "LLLDLDD", "antigo": "LLLDDDD"}


def _consenso_caractere(leituras: list[tuple[str, float]], formato: str | None = None) -> str | None:
    """Consenso por POSIÇÃO de caractere, ponderado por confiança (padrão de mercado ALPR).

    Combina várias leituras (de múltiplos frames E engines) votando cada posição
    separadamente — corrige erros de 1 caractere: se 2 frames leem 'ABC1D23' e 1 lê
    'ABC1O23', a posição 5 elege 'D'. Considera só placas de 7 chars (padrão BR).

    `formato` ('mercosul'/'antigo'): quando o tipo visual é conhecido (faixa azul do
    Mercosul), restringe cada posição ao TIPO esperado — na posição 5 do Mercosul só
    conta votos de LETRA, descartando dígitos como erro de OCR (em vez de chutar 2→Z).
    Recupera a letra certa de outro frame. Se nenhuma leitura deu o tipo certo numa
    posição, cai para o voto bruto daquela posição.
    """
    validas = [(p, max(w, 0.01)) for p, w in leituras if p and len(p) == 7]
    if not validas:
        return None
    padrao = _PADRAO_POS.get(formato or "")
    consenso = []
    for i in range(7):
        votos: dict[str, float] = defaultdict(float)
        if padrao:
            tipo = padrao[i]
            for p, w in validas:
                ch = p[i]
                if tipo == "L" and not ch.isalpha():
                    continue          # espera letra, veio dígito → descarta (erro)
                if tipo == "D" and not ch.isdigit():
                    continue          # espera dígito, veio letra → descarta
                votos[ch] += w
        if not votos:                 # sem formato, ou nenhum voto do tipo certo
            for p, w in validas:
                votos[p[i]] += w
        consenso.append(max(votos.items(), key=lambda kv: kv[1])[0])
    return "".join(consenso)


def _eleger_placa(candidatos: list[dict]) -> dict | None:
    """Elege a placa final por consenso de caractere entre TODOS os candidatos acumulados.

    Reusada tanto pela checagem de parada antecipada do loop de leitura (a cada frame)
    quanto pela decisão final — garante que o resultado não muda dependendo de quando o
    loop parou, só a quantidade de evidência acumulada até ali.
    Retorna None se `candidatos` estiver vazio. O dict retornado inclui as chaves extras
    "acordo" (concordância 0-1 da placa eleita) e "n_votos_snap" (fotos que bateram nela).
    """
    if not candidatos:
        return None

    # Pool de leituras: a placa final de cada candidato + cada engine individual,
    # ponderadas por confiança. Vota-se cada posição de caractere separadamente.
    leituras: list[tuple[str, float]] = []
    for c in candidatos:
        leituras.append((c["placa"], float(c["confianca"])))
        for d in c.get("detalhes_ocr", []):
            if d.get("placa"):
                leituras.append((d["placa"], float(d.get("confianca", 0.5))))

    # Formato visual predominante (Mercosul/antigo) detectado pelos engines — usado como
    # prior para restringir o consenso por posição (posição 5 do Mercosul = letra).
    fmt_votos = Counter(c["padrao"] for c in candidatos if c.get("padrao"))
    formato_prior = fmt_votos.most_common(1)[0][0] if fmt_votos else None

    placa_consenso = _consenso_caractere(leituras, formato=formato_prior)
    votos_placa = Counter(c["placa"] for c in candidatos)
    if placa_consenso and validar(placa_consenso):
        placa_eleita = placa_consenso           # consenso por caractere (corrige 1-char)
    else:
        placa_eleita = votos_placa.most_common(1)[0][0]   # fallback: string mais votada

    n_votos_snap = sum(1 for c in candidatos if c["placa"] == placa_eleita)
    # Melhor candidato (p/ crop/bbox): o da placa eleita, senão o de maior confiança
    cands_eleita = [c for c in candidatos if c["placa"] == placa_eleita]
    melhor = dict(max(cands_eleita or candidatos, key=lambda c: c["confianca"]))
    melhor["placa"] = placa_eleita
    _v = validar(placa_eleita)
    if _v:
        melhor["padrao"] = _v[1]

    # Concordância: fração do peso das leituras que bateu com a placa eleita — usada tanto
    # para escalar a confiança final quanto como sinal de parada antecipada do loop.
    peso_total = sum(w for _, w in leituras)
    acordo = sum(w for p, w in leituras if p == placa_eleita) / max(peso_total, 1e-6)
    melhor["confianca"] = round(melhor["confianca"] * max(acordo, 0.34), 3)
    melhor["acordo"] = round(acordo, 3)
    melhor["n_votos_snap"] = n_votos_snap
    return melhor


def _mesclar_com_anterior(melhor: dict, anterior: dict) -> dict:
    """Combina a placa desta leitura com a última detecção do MESMO bico, ainda dentro
    do cooldown, por consenso de caractere.

    O roteador costuma acionar o mesmo bico várias vezes seguidas para o mesmo veículo
    (reflexo do bico, retry dele). `_eleger_placa` só vê as fotos de UMA chamada — aqui
    o resultado já votado da chamada anterior entra como mais uma leitura, do jeito que
    entraria se tivesse vindo do mesmo loop. Sem isso, ruído de 1-2 caracteres entre
    chamadas sucessivas virava uma segunda linha diferente no histórico para o mesmo carro.
    """
    leituras = [(melhor["placa"], melhor["confianca"]), (anterior["placa"], anterior["confianca"])]
    consenso = _consenso_caractere(leituras, formato=melhor.get("padrao"))
    if consenso and validar(consenso):
        placa_final = consenso
    else:
        placa_final = melhor["placa"] if melhor["confianca"] >= anterior["confianca"] else anterior["placa"]

    fundido = dict(melhor)
    fundido["placa"] = placa_final
    v = validar(placa_final)
    if v:
        fundido["padrao"] = v[1]
    fundido["confianca"] = round(max(melhor["confianca"], anterior["confianca"]), 3)
    return fundido


def _mesclar_com_historico(
    melhor: dict,
    bico_id: int,
    camera_id: int,
    origem: str,
    cooldown_seg: float,
) -> tuple[dict, int | None]:
    """Cruza esta leitura com o histórico recente e decide se ela é o MESMO veículo.

    Devolve `(melhor, anterior_id)`: com `anterior_id` preenchido, quem chama deve
    ATUALIZAR aquela linha em vez de inserir uma nova — o histórico mostra um evento por
    veículo, não um por chamada do roteador.

    Teste manual (botão da tela de ROI/posto) fica de fora, por duas razões:

    1. Quem está ajustando enquadramento aperta o botão várias vezes de propósito e
       precisa ver uma linha por aperto — mesclar esconderia exatamente a comparação
       entre tentativas que motivou o teste.
    2. Mais grave: o ramo `else` abaixo ABSORVE a detecção do 'pipeline' e APAGA a linha
       original. Essa absorção foi escrita para a leitura do roteador, que é o evento com
       significado de negócio; disparada por um teste, ela deletava do histórico uma
       detecção real do monitoramento contínuo só porque alguém foi conferir a câmera.
    """
    if origem == "teste":
        return melhor, None

    desde = (datetime.now(timezone.utc) - timedelta(seconds=cooldown_seg)).isoformat()

    # Mesmo bico, ainda dentro do cooldown, placa parecida: o roteador aciona de novo pro
    # mesmo veículo (retry dele, novo pulso do bico) e sem isto cada chamada emitia sua
    # própria linha, mesmo quando só 1-2 caracteres divergiam por ruído de OCR.
    anterior = banco.ultima_deteccao_bico(bico_id, desde, origem)
    if anterior and parecidas(anterior["placa"], melhor["placa"], max_diff=3):
        return _mesclar_com_anterior(melhor, anterior), anterior["id"]

    # Sem match no mesmo bico: cruza com o 'pipeline' (monitoramento contínuo da MESMA
    # câmera física, sem bico_id) — o mesmo veículo é comum aparecer nos dois quase ao
    # mesmo tempo. A leitura reativa é o evento com significado de negócio (ligada ao
    # bico), então ABSORVE o 'pipeline' em vez do contrário: some com aquela linha e
    # grava só a reativa.
    pipeline_anterior = banco.ultima_deteccao_camera(camera_id, desde, origem="pipeline")
    if pipeline_anterior and parecidas(pipeline_anterior["placa"], melhor["placa"], max_diff=3):
        melhor = _mesclar_com_anterior(melhor, pipeline_anterior)
        banco.remover_deteccao(pipeline_anterior["id"])
    return melhor, None


def _detectar(det_inst, frame, roi: dict | None, lock: threading.Lock):
    """Detecta placas no frame, recortando por ROI antes (mesmo padrão de
    Pipeline._processar_frame) quando o bico tem uma área própria configurada.
    """
    if roi:
        rx, ry, rw, rh = roi["x"], roi["y"], roi["w"], roi["h"]
        frame_det = frame[ry:ry + rh, rx:rx + rw]
        if frame_det.size == 0:
            return []
        with lock:
            bboxes_roi = det_inst.detectar(frame_det)
        return [(x + rx, y + ry, w, h, c) for x, y, w, h, c in bboxes_roi]
    with lock:
        return det_inst.detectar(frame)


# ── Lock por câmera ──────────────────────────────────────────────────────────
# Evita que 2 bicos compartilhando a MESMA câmera abram conexões RTSP simultâneas
# (câmeras Intelbras só toleram 1 conexão por vez). Câmeras diferentes seguem em
# paralelo — o lock é por camera_id.
#
# ATENÇÃO à latência: o lock cobre a leitura INTEIRA (conexão + loop reject-retry),
# não só o connect, porque a conexão RTSP fica aberta durante todo o loop. Logo, dois
# bicos da mesma câmera acionados ao mesmo tempo são serializados: o segundo espera o
# primeiro terminar, e no pior caso a resposta dele leva ~2x `leitura_timeout_seg`.
# Com o padrão de 28s isso estoura a tolerância de ~25-30s do roteador. Mitigações se
# isso aparecer em campo: baixar `leitura_timeout_seg`, ou dar um timeout de aquisição
# ao lock e responder 503 rápido em vez de enfileirar.
_locks_camera: dict[int, threading.Lock] = {}
_locks_camera_guarda = threading.Lock()


def _obter_lock_camera(camera_id: int) -> threading.Lock:
    with _locks_camera_guarda:
        lock = _locks_camera.get(camera_id)
        if lock is None:
            lock = threading.Lock()
            _locks_camera[camera_id] = lock
        return lock


def lock_camera(camera_id: int) -> threading.Lock:
    """Lock público desta câmera — usado por quem também abre conexão direta (ex.: o
    snapshot do editor de ROI), para não competir com uma leitura em andamento."""
    return _obter_lock_camera(camera_id)


def ler_placa(**kw) -> dict:
    """Rotula com a câmera de origem tudo que a leitura logar, inclusive o que sai do
    fundo do OCR — sem isso as linhas da leitura reativa entram sem dono no mesmo arquivo
    dos pipelines contínuos. Ver app/visao/contexto_log.py. Envelope fino de propósito:
    o corpo é keyword-only, então não há assinatura para duplicar aqui."""
    with contexto_log.usar(camera=kw.get("camera_id")):
        return _ler_placa(**kw)


def _ler_placa(
    *,
    camera_id: int,
    especificacao: EspecificacaoCamera,
    roi: dict | None,
    cfg: dict,
    pipeline_frame_provider: Callable[[], np.ndarray | None] | None = None,
    preview_nome: str,
    bico_id: int | None = None,
    origem: str = "roteador",
) -> dict:
    """Loop de leitura por confiança ("reject-retry", padrão de mercado ALPR): tira fotos
    incrementalmente e para assim que o consenso entre as leituras ficar forte o bastante
    (ou ao atingir o máximo de tentativas/timeout) — em vez de um número fixo de fotos.

    `pipeline_frame_provider`: quando fornecido, tenta reusar o frame LIMPO de um pipeline
    contínuo já ativo para essa câmera (sem abrir segunda conexão RTSP); cai para conexão
    direta se `None` ou se o pipeline ainda não tiver frame. O modo reativo multi-tenant
    passa sempre `None` — a foto tem que ser fresca, tirada agora, nunca reaproveitada.
    """
    from app.visao.detector import obter_detector_leitura, detector_leitura_lock
    from app.visao.ocr import obter_ocr_leitura, ocr_leitura_lock

    deteccao_auto = cfg.get("deteccao_automatica", "sim").lower() in ("sim", "true", "1")
    n_min = max(1, int(cfg.get("snapshots_votacao", "3")))
    n_max = max(n_min, int(cfg.get("leitura_max_tentativas", "12")))
    timeout_seg = float(cfg.get("leitura_timeout_seg", "6"))
    acordo_min = float(cfg.get("leitura_acordo_minimo", "0.80"))

    # Cobre a chamada INTEIRA, inclusive esperas antes do laço (pipeline_frame_provider,
    # lock de câmera, conexão RTSP) — é a referência para o orçamento de tempo real que o
    # roteador sente. Ajustada mais abaixo para excluir só a carga de modelo (linha ~344),
    # não essas esperas: diferente da carga (custo único pós-boot), elas são latência real
    # de cada chamada e precisam contar contra `timeout_seg`, senão o total de ponta a
    # ponta pode passar bem além do configurado sem o laço perceber.
    inicio_absoluto = time.time()

    usar_pipeline = pipeline_frame_provider is not None and deteccao_auto
    frame_inicial = pipeline_frame_provider() if usar_pipeline else None
    if usar_pipeline and frame_inicial is None:
        usar_pipeline = False

    cam_lock = None if usar_pipeline else _obter_lock_camera(camera_id)
    if cam_lock is not None:
        cam_lock.acquire()

    # O pipeline contínuo já aplica AjustadorAmbiente antes de publicar o frame "limpo"
    # que `pipeline_frame_provider` devolve — mas a conexão DIRETA (usada sempre que o
    # pipeline não está disponível: câmera reconectando, deteccao_automatica=nao, etc.)
    # não passava por nenhum ajuste. É justamente nesses momentos de instabilidade que a
    # robustez a iluminação mais importaria, então instanciamos aqui também.
    ajustador_ambiente = None
    if not usar_pipeline:
        from app.visao.ambiente import AjustadorAmbiente
        ajustador_ambiente = AjustadorAmbiente(cfg, camera_db_id=camera_id)

    camera_direta: Camera | None = None
    candidatos: list[dict] = []
    # Quantos recortes o DETECTOR entregou ao longo do loop, mesmo os que o OCR recusou.
    # Sem isso, "detector não viu placa nenhuma" e "viu, mas o OCR não leu" chegavam ao
    # usuário como a mesma mensagem — e são problemas opostos (enquadramento/modelo de
    # detecção vs resolução/nitidez da placa), que se resolvem de formas diferentes.
    bboxes_total = 0
    frame_principal = None       # melhor (mais nítido) frame já visto — usado no preview
    nitidez_principal = -1.0
    tentativas = 0
    parada_motivo = "max_tentativas"
    inicio = inicio_absoluto

    try:
        if not usar_pipeline:
            intelbras = {
                "host": especificacao.intelbras_host,
                "porta": especificacao.intelbras_porta,
                "usuario": especificacao.intelbras_usuario,
                "senha": especificacao.intelbras_senha,
                "canal": especificacao.intelbras_canal,
                "subtype": especificacao.intelbras_subtype,
                "formato": especificacao.intelbras_formato,
                "rtsp_transporte": cfg.get("rtsp_transporte", "tcp"),
            }
            if especificacao.rtsp_url_custom:
                intelbras["host"] = ""
            try:
                camera_direta = Camera(
                    tipo=especificacao.camera_tipo,
                    indice=especificacao.rtsp_url_custom or especificacao.camera_indice,
                    largura=int(cfg.get("camera_largura", "1280")),
                    altura=int(cfg.get("camera_altura", "720")),
                    fps=int(cfg.get("camera_fps", "15")),
                    intelbras=intelbras,
                )
                camera_direta.abrir()
            except Exception as e:
                log.warning("ler-placa camera_id=%d bico_id=%s: %s", camera_id, bico_id, e)
                tipo_cam = especificacao.camera_tipo
                host = especificacao.intelbras_host or especificacao.rtsp_url_custom
                # Remove credenciais de URLs RTSP antes de expor na mensagem de erro
                host_safe = re.sub(r"(rtsp?://)[^@]+@", r"\1***:***@", host)
                if tipo_cam in ("rtsp", "intelbras") or host:
                    detalhe = f" ({host_safe})" if host_safe else ""
                    raise LeituraError(
                        503,
                        f"Não foi possível conectar à câmera via RTSP{detalhe}. "
                        "Verifique o IP/host, porta, usuário e senha.",
                    )
                raise LeituraError(503, "Falha ao abrir câmera.")

            # Aguarda o primeiro frame válido (até 15s)
            for _ in range(150):
                frame_inicial = camera_direta.ler()
                if frame_inicial is not None:
                    break
                time.sleep(0.1)
            if frame_inicial is None:
                camera_direta.fechar()
                raise LeituraError(503, "Câmera conectou mas não enviou frames — verifique a conexão")
            if ajustador_ambiente is not None and ajustador_ambiente.ativo:
                frame_inicial = ajustador_ambiente.processar(frame_inicial)

        # ── Detector e OCR ────────────────────────────────────────────────────
        # Leitura sob demanda usa componentes de ALTA PRECISÃO, independentes do stream ao
        # vivo: detecção 2 estágios veículo→placa (obter_detector_leitura) + OCR com reforço
        # PaddleOCR (obter_ocr_leitura). Ambos toleram a latência maior do fluxo sob demanda.
        t_antes_modelo = time.time()
        det_inst = obter_detector_leitura(cfg)
        ocr_inst = obter_ocr_leitura(cfg)
        tempo_carga_modelo = time.time() - t_antes_modelo

        # Desloca a referência do orçamento só pelo tempo de CARGA DE MODELO: na primeira
        # leitura após subir o servidor, isso leva dezenas de segundos e não deve consumir
        # o orçamento (custo único, não repete nas próximas chamadas). A espera anterior
        # (pipeline/lock/conexão) continua contando — ver comentário em `inicio_absoluto`.
        inicio = inicio_absoluto + tempo_carga_modelo

        # ── Loop de leitura: acumula candidatos até o consenso ficar forte ──────
        while tentativas < n_max:
            if time.time() - inicio > timeout_seg:
                parada_motivo = "timeout"
                break

            if tentativas == 0:
                frame = frame_inicial
            elif usar_pipeline:
                frame = pipeline_frame_provider()
            else:
                frame = camera_direta.ler()
                if frame is not None and ajustador_ambiente is not None and ajustador_ambiente.ativo:
                    frame = ajustador_ambiente.processar(frame)

            if frame is None:
                time.sleep(0.1)
                continue
            tentativas += 1

            # Melhor frame p/ preview = o mais nítido entre os capturados (Laplaciano).
            cinza = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            nitidez = cv2.Laplacian(cinza, cv2.CV_64F).var()
            if nitidez > nitidez_principal:
                nitidez_principal = nitidez
                frame_principal = frame

            # Locks: det_inst/ocr_inst são instâncias CACHEADAS compartilhadas entre
            # requests concorrentes (2+ bicos podem "Ler Placa" ao mesmo tempo). Em
            # CUDAExecutionProvider (GPU), chamadas concorrentes na mesma sessão onnxruntime
            # podem travar/crashar — o lock serializa só a chamada individual, não o loop
            # inteiro, pra não bloquear um bico pela duração toda da leitura do outro.
            bboxes = _detectar(det_inst, frame, roi, detector_leitura_lock)
            bboxes_total += len(bboxes)
            f_h, f_w = frame.shape[:2]
            for x, y, w, h, conf_det in bboxes:
                x, y, w, h = _expandir_bbox(x, y, w, h, f_w, f_h)
                crop = frame[y: y + h, x: x + w]
                if crop.size == 0:
                    continue

                if hasattr(ocr_inst, "ler_detalhado"):
                    with ocr_leitura_lock:
                        ocr_res = ocr_inst.ler_detalhado(crop)
                    if not ocr_res["placa"]:
                        continue
                    placa      = ocr_res["placa"]
                    padrao     = ocr_res["padrao"]
                    conf_ocr   = ocr_res["confianca"]
                    votos_ocr  = ocr_res["votos"]
                    total_eng  = ocr_res["total_engines"]
                    det_ocr    = ocr_res["detalhes"]
                else:
                    with ocr_leitura_lock:
                        texto, conf_ocr = ocr_inst.ler(crop)
                    resultado = validar(texto)
                    if not resultado:
                        continue
                    placa, padrao = resultado
                    votos_ocr = 1
                    total_eng = 1
                    det_ocr   = [{"engine": getattr(ocr_inst, "engine", "?"), "placa": placa,
                                   "padrao": padrao, "confianca": round(conf_ocr, 3)}]

                candidatos.append({
                    "placa":         placa,
                    "padrao":        padrao,
                    "confianca":     round((conf_det + conf_ocr) / 2, 3),
                    "votos_ocr":     votos_ocr,
                    "total_engines": total_eng,
                    "detalhes_ocr":  det_ocr,
                    "crop":          crop,
                    "bbox":          {"x": x, "y": y, "w": w, "h": h},
                    "frame":         frame,
                    "snapshot_idx":  tentativas - 1,
                    # Estimativa moto/carro do AutoOCR deste crop (None = engine único,
                    # que não calcula, ou sem header para decidir). Vem por candidato e
                    # não da última chamada ao OCR porque `_eleger_placa` pode eleger um
                    # candidato que não é o último analisado — ler o atributo depois do
                    # laço gravaria o tipo do crop errado.
                    "tipo_veiculo":  getattr(ocr_inst, "_ultimo_tipo_veiculo", None),
                })

            # Parada antecipada: só depois do mínimo de fotos, e só se o consenso for forte
            # o bastante (evita parar num acerto isolado de sorte na 1ª foto).
            #
            # `n_votos_snap >= 2` é essencial aqui, não redundante com `acordo >= acordo_min`:
            # o pool de "leituras" do _eleger_placa mistura a placa de cada candidato COM
            # cada engine individual dele (linha ~131). Para carro com boa confiança, AutoOCR
            # nem roda o engine de fallback (só 1 entrada em detalhes_ocr) — se só 1 dos
            # frames capturados até agora produziu detecção válida, esse único candidato
            # entra 2x no pool e "acordo" fecha em 1.0 sozinho, sem nenhuma concordância
            # ENTRE frames de verdade. Sem essa segunda checagem, o loop parava ali,
            # abrindo mão do resto do orçamento de tempo/tentativas que poderia confirmar
            # (ou contradizer) essa única leitura.
            if tentativas >= n_min and candidatos:
                eleito_parcial = _eleger_placa(candidatos)
                if (eleito_parcial and eleito_parcial["acordo"] >= acordo_min
                        and eleito_parcial["n_votos_snap"] >= 2):
                    parada_motivo = "acordo"
                    break

            if tentativas < n_max:
                time.sleep(0.15 if usar_pipeline else 0.5)
    finally:
        if camera_direta is not None:
            camera_direta.fechar()
        if cam_lock is not None:
            cam_lock.release()

    if frame_principal is None:
        # Distingue "a câmera não entregou imagem" de "o tempo acabou antes de tentar" —
        # antes as duas situações davam a mesma mensagem, culpando a câmera à toa.
        if parada_motivo == "timeout" and tentativas == 0:
            raise LeituraError(
                503,
                f"Tempo esgotado ({timeout_seg:.0f}s) antes de analisar qualquer imagem. "
                "Pode ter sido demora para conectar à câmera/pipeline, ou (se foi logo após "
                "reiniciar o servidor) carga inicial de modelo — tente de novo. Caso "
                "persista, aumente `leitura_timeout_seg`.",
            )
        raise LeituraError(503, "Câmera conectou mas não enviou frames — verifique a conexão")

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    melhor = _eleger_placa(candidatos) if candidatos else None

    anterior_id: int | None = None
    if melhor is not None and bico_id is not None:
        melhor, anterior_id = _mesclar_com_historico(
            melhor, bico_id=bico_id, camera_id=camera_id, origem=origem,
            cooldown_seg=float(cfg.get("cooldown_seg", "120")),
        )

    # Preview: quando houve leitura, mostra o FRAME DE ONDE a placa vencedora saiu, com a
    # caixa exata que o OCR usou. Antes rodava uma segunda detecção sobre o frame mais
    # nítido — custava uma passada inteira do detector e podia desenhar caixa diferente
    # (ou nenhuma) da que foi realmente lida, o que atrapalha auditar uma leitura errada.
    if melhor is not None:
        frame_preview = melhor["frame"].copy()
        bb = melhor["bbox"]
        cv2.rectangle(frame_preview, (bb["x"], bb["y"]),
                      (bb["x"] + bb["w"], bb["y"] + bb["h"]), (0, 200, 255), 2)
        cv2.putText(frame_preview, melhor["placa"], (bb["x"], max(bb["y"] - 8, 14)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 255), 2)
    else:
        frame_preview = frame_principal.copy()
    # Marca a área do bico, para dar para conferir o enquadramento junto com o resultado
    if roi:
        cv2.rectangle(frame_preview, (roi["x"], roi["y"]),
                      (roi["x"] + roi["w"], roi["y"] + roi["h"]), (120, 120, 120), 1)

    cv2.imwrite(str(SNAPSHOT_DIR / f"{preview_nome}.jpg"), frame_preview,
                [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    frame_url = f"/static/snapshots/{preview_nome}.jpg"

    # `melhor` só é None quando `candidatos` está vazio (_eleger_placa sempre elege algo a
    # partir de um candidato), então os dois casos de falha se separam por `bboxes_total`:
    # o detector não achou placa, ou achou e nenhum recorte virou texto válido.
    if not candidatos:
        if bboxes_total:
            mensagem = (f"Placa localizada em {bboxes_total} recorte(s), mas o texto não foi "
                        "reconhecido — placa pequena, borrada ou muito inclinada para o OCR")
        else:
            mensagem = ("Nenhuma placa detectada nos frames — verifique o enquadramento da "
                        "área do bico e se o veículo aparece dentro dela")
        return {"placa": None, "mensagem": mensagem, "frame_url": frame_url,
                "camera_id": camera_id, "bico_id": bico_id, "bboxes_detectadas": bboxes_total,
                "snapshots_analisados": tentativas, "tentativas": tentativas, "parada_motivo": parada_motivo}

    n_votos_snap = melhor.pop("n_votos_snap")
    acordo_final = melhor.pop("acordo")

    confirmada = _confirmada(acordo_final, n_votos_snap, acordo_min, n_min)

    # Tipo estimado do candidato ELEITO (`_eleger_placa` devolve uma cópia dele, então a
    # chave vem junto). `.get` e não indexação: `_mesclar_com_anterior`/`_mesclar_com_
    # historico` remontam o dict a partir da leitura anterior e podem não trazer a chave.
    tipo_veiculo = melhor.get("tipo_veiculo")

    # ── Quadro inteiro desta detecção ─────────────────────────────────────────
    # O preview acima é sobrescrito a cada leitura; aqui guardamos uma cópia com nome
    # único, para o histórico poder mostrar o contexto (qual veículo, onde estava)
    # e não só o recorte da placa.
    frame_rel = None
    if cfg.get("salvar_frame_deteccao", "sim").lower() in ("sim", "true", "1"):
        ts_f = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        nome_f = f"{ts_f}_{melhor['placa']}_frame.jpg"
        cv2.imwrite(str(SNAPSHOT_DIR / nome_f), frame_preview,
                    [int(cv2.IMWRITE_JPEG_QUALITY), int(cfg.get("snapshot_qualidade", "85"))])
        frame_rel = f"/static/snapshots/{nome_f}"

    # ── Snapshot do crop ──────────────────────────────────────────────────────
    snapshot_rel = None
    if cfg.get("salvar_snapshot", "").lower() in ("sim", "true", "1"):
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        nome = f"{ts}_{melhor['placa']}.jpg"
        cv2.imwrite(str(SNAPSHOT_DIR / nome), melhor["crop"],
                    [int(cv2.IMWRITE_JPEG_QUALITY), int(cfg.get("snapshot_qualidade", "85"))])
        snapshot_rel = f"/static/snapshots/{nome}"

    # ── Persiste ──────────────────────────────────────────────────────────────
    # Se mesclou com a detecção anterior do mesmo bico, ATUALIZA aquela linha em vez de
    # inserir uma nova — o histórico mostra um evento por veículo, não um por chamada.
    if anterior_id is not None:
        banco.atualizar_deteccao(
            anterior_id, placa=melhor["placa"], padrao=melhor["padrao"],
            confianca=melhor["confianca"], snapshot=snapshot_rel, frame=frame_rel,
            acordo=acordo_final, confirmada=confirmada, tipo_veiculo=tipo_veiculo,
        )
        det_id = anterior_id
    else:
        det_id = banco.registrar_deteccao(
            placa=melhor["placa"], padrao=melhor["padrao"], confianca=melhor["confianca"],
            snapshot=snapshot_rel, camera_id=especificacao.camera_tipo, bbox=melhor["bbox"],
            bico_id=bico_id, frame=frame_rel, origem=origem, camera_db_id=camera_id,
            acordo=acordo_final, confirmada=confirmada, tipo_veiculo=tipo_veiculo,
        )
    estado.adicionar_deteccao({
        "id": det_id, "placa": melhor["placa"], "padrao": melhor["padrao"],
        "confianca": melhor["confianca"], "snapshot": snapshot_rel,
        "criado_em": datetime.now(timezone.utc).isoformat(),
        # Sem isto o painel de recentes mostraria uma leitura fraca idêntica a uma
        # sólida, contradizendo o que ficou gravado no banco.
        "acordo": acordo_final, "confirmada": confirmada,
        "tipo_veiculo": tipo_veiculo,
    })
    log.info("Ler-placa: %s (%s, conf=%.2f, acordo=%.2f%s, tentativas=%d/%d, parada=%s, ocr=%d/%d, "
             "camera_id=%d, bico_id=%s)",
             melhor["placa"], melhor["padrao"], melhor["confianca"], acordo_final,
             "" if confirmada else " NAO-CONFIRMADA",
             tentativas, n_max, parada_motivo, melhor["votos_ocr"], melhor["total_engines"],
             camera_id, bico_id)

    return {
        "camera_id":           camera_id,
        "bico_id":             bico_id,
        "placa":               melhor["placa"],
        "padrao":              melhor["padrao"],
        "confianca":           melhor["confianca"],
        "votos_snapshot":      n_votos_snap,
        "total_snapshots":     tentativas,
        "votos_ocr":           melhor["votos_ocr"],
        "total_engines":       melhor["total_engines"],
        "detalhes_ocr":        melhor["detalhes_ocr"],
        "snapshot":            snapshot_rel,
        "frame_url":           frame_url,
        "tentativas":          tentativas,
        "acordo":              acordo_final,
        "confirmada":          confirmada,
        "parada_motivo":       parada_motivo,
        "tipo_veiculo":        tipo_veiculo,
    }
