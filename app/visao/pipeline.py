"""Loop principal: captura → detecta → OCR → valida → persiste."""
from __future__ import annotations
import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2

from app.core import banco
from app.core import broadcaster as bc
from app.core import estado
from app.visao.camera import Camera
from app.visao.detector import Detector
from app.visao.ocr import OCR
from app.visao.validador import parecidas, validar

log = logging.getLogger(__name__)

SNAPSHOT_DIR = Path("app/web/static/snapshots")

# Padding lateral/inferior do bbox YOLO antes do crop.
# O YOLO detecta a área branca da placa (os números) — expandir para CIMA
# inclui a faixa azul "BRASIL" do cabeçalho Mercosul e quebra o OCR.
# Por isso: padding só nas laterais e na base, NUNCA para cima.
BBOX_PADDING = 0.05


def _expandir_bbox(x: int, y: int, w: int, h: int, frame_w: int, frame_h: int,
                   fator: float = BBOX_PADDING) -> tuple[int, int, int, int]:
    dx = int(w * fator)
    dy = int(h * fator)
    x2 = max(0, x - dx)       # expande esquerda
    y2 = max(0, y)             # NÃO expande para cima — evita header azul Mercosul
    w2 = min(frame_w - x2, w + 2 * dx)

    # Bbox muito achatado (w/h > 4): YOLO detectou só o header da placa antiga
    # (ex: "UF-CIDADE"). Estende até o final do frame para incluir os caracteres.
    if w / max(h, 1) > 4:
        h2 = frame_h - y2
    else:
        h2 = min(frame_h - y2, h + dy)   # expande só para baixo
    return x2, y2, w2, h2


class Pipeline:
    def __init__(self, cfg: dict[str, str], camera_db_id: int = 0):
        self.camera_db_id = camera_db_id
        self.cfg = cfg
        self.camera = Camera(
            tipo=cfg["camera_tipo"],
            indice=cfg["camera_indice"],
            largura=int(cfg["camera_largura"]),
            altura=int(cfg["camera_altura"]),
            fps=int(cfg["camera_fps"]),
            intelbras={
                "host": cfg.get("intelbras_host", ""),
                "porta": cfg.get("intelbras_porta", "554"),
                "usuario": cfg.get("intelbras_usuario", "admin"),
                "senha": cfg.get("intelbras_senha", ""),
                "canal": cfg.get("intelbras_canal", "1"),
                "subtype": cfg.get("intelbras_subtype", "1"),
                "formato": cfg.get("intelbras_formato", "padrao"),
            },
        )
        from app.visao.detector import criar_detector
        self.detector = criar_detector(cfg)
        _engine = cfg.get("ocr_engine", "tesseract")
        _psm = int(cfg["tesseract_psm"])
        _deskew_on = cfg.get("deskew_ativo", "sim").lower() in ("sim", "true", "1", "yes")
        _deskew_max = float(cfg.get("deskew_angulo_max", "30"))
        extras = [e.strip() for e in cfg.get("ocr_engines_extra", "").split(",") if e.strip()]
        if _engine == "auto":
            from app.visao.ocr import AutoOCR
            self.ocr = AutoOCR(tesseract_psm=_psm,
                               deskew_ativo=_deskew_on, deskew_angulo_max=_deskew_max)
        elif extras:
            from app.visao.ocr import MultiOCR
            self.ocr = MultiOCR(engines=[_engine] + extras, tesseract_psm=_psm,
                                deskew_ativo=_deskew_on, deskew_angulo_max=_deskew_max)
        else:
            self.ocr = OCR(engine=_engine, tesseract_psm=_psm,
                           deskew_ativo=_deskew_on, deskew_angulo_max=_deskew_max)
        self.votos_minimos = max(1, int(cfg.get("ocr_votos_minimos", "1")))
        self.frames_consenso = int(cfg["frames_consenso"])
        self.cooldown_seg = int(cfg["cooldown_seg"])
        self.skip_n = max(1, int(cfg.get("processar_a_cada_n_frames", "2")))
        self._intervalo_deteccao = 1.0 / max(1, int(cfg.get("deteccao_fps_max", "5")))
        self.salvar_snapshot = cfg["salvar_snapshot"].lower() in ("sim", "true", "1")
        self.snapshot_q = int(cfg["snapshot_qualidade"])
        self.deteccao_automatica = cfg.get("deteccao_automatica", "sim").lower() in ("sim", "true", "1")

        self.bomba = int(cfg.get("bomba", "0"))
        self.lado = int(cfg.get("lado", "0"))

        # Ajuste adaptativo de imagem por condição de ambiente (no-op se desativado).
        from app.visao.ambiente import AjustadorAmbiente
        self.ajustador = AjustadorAmbiente(cfg, camera_db_id=camera_db_id)

        import json as _json
        roi_raw = cfg.get("roi")
        self.roi: dict | None = _json.loads(roi_raw) if roi_raw else None

        self._historico: deque = deque(maxlen=10)
        self._parar = threading.Event()
        self._thread: threading.Thread | None = None
        # True enquanto `iniciar()` não terminou. A instância é publicada em `_instancias`
        # antes disso (para sinalizar que a câmera já está ocupada), e sem esta marca o
        # supervisor veria um pipeline sem thread e o reiniciaria em loop.
        self.iniciando: bool = True
        self._ultima_deteccao: float = 0.0

        # ByteTrack — instanciado sempre; ativo só se boxmot estiver instalado
        _tracker_on = cfg.get("tracker_ativo", "sim").lower() in ("sim", "true", "1")
        if _tracker_on:
            from app.visao.tracker import Tracker as _Tracker
            self.tracker: _Tracker | None = _Tracker(
                ocr_a_cada_n_frames=int(cfg.get("tracker_ocr_intervalo", "5")),
                votos_emitir=int(cfg.get("tracker_votos_emitir", "2")),
                paciencia_frames=int(cfg.get("tracker_paciencia_frames", "40")),
            )
        else:
            self.tracker = None

    def iniciar(self) -> None:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        estado.camera_tipo = self.cfg["camera_tipo"]
        if self.deteccao_automatica:
            # Mesmo lock por câmera que a leitura reativa/direta usa (app/visao/leitura.py)
            # — sem isso, abrir a conexão RTSP do pipeline podia coincidir com uma leitura
            # reativa da MESMA câmera abrindo conexão direta ao mesmo tempo (ex.: pipeline
            # subindo enquanto um bico já chama), e a Intelbras só tolera 1 conexão por vez.
            # Import local para evitar ciclo (leitura.py importa `_expandir_bbox` daqui).
            from app.visao.leitura import lock_camera
            try:
                with lock_camera(self.camera_db_id):
                    self.camera.abrir()
                estado.camera_conectada = True
            except Exception as e:
                log.error("Câmera não encontrada ao iniciar (%s) — pipeline aguardará reconexão", e)
                estado.camera_conectada = False
        self.detector.carregar()
        estado.modelo_carregado = self.detector.sess is not None
        self.ocr.carregar()
        estado.ocr_engine_ativo = self.ocr.engine
        if self.tracker is not None:
            self.tracker.carregar()
        self._parar.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="alpr-pipeline")
        self._thread.start()
        self.iniciando = False
        estado.pipeline_rodando = True
        log.info("Pipeline iniciado (modo=%s)", "automático" if self.deteccao_automatica else "manual")

    def parar(self) -> None:
        self._parar.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.camera.fechar()
        estado.pipeline_rodando = False
        estado.camera_conectada = False
        log.info("Pipeline parado")

    def _loop(self) -> None:
        if not self.deteccao_automatica:
            while not self._parar.is_set():
                time.sleep(1.0)
            return

        ultimo_fps = time.time()
        frames_count = 0
        sem_frame_desde: float | None = None
        _intervalo_loop = 1.0 / max(1, int(self.cfg.get("camera_fps", "15")))

        while not self._parar.is_set():
            try:
                t_loop = time.time()
                frame = self.camera.ler()

                if frame is None:
                    agora = time.time()
                    if sem_frame_desde is None:
                        sem_frame_desde = agora
                        log.warning("Câmera %d sem frame — aguardando...", self.camera_db_id)
                    elif agora - sem_frame_desde >= 3.0:
                        log.warning("Câmera %d sem resposta por 3s — tentando reconectar...", self.camera_db_id)
                        estado.camera_conectada = False
                        # Mesmo lock de `iniciar()` acima — a reconexão também abre RTSP
                        # do zero, e pode coincidir com uma leitura reativa da mesma
                        # câmera caindo para conexão direta nesse exato momento instável.
                        from app.visao.leitura import lock_camera
                        with lock_camera(self.camera_db_id):
                            reconectou = self.camera.reconectar()
                        if reconectou:
                            estado.camera_conectada = True
                            sem_frame_desde = None
                            log.info("Câmera %d reconectada", self.camera_db_id)
                        else:
                            log.warning("Câmera %d: reconexão falhou — nova tentativa em 30s", self.camera_db_id)
                            sem_frame_desde = time.time()
                            time.sleep(30)
                    else:
                        time.sleep(0.1)
                    continue

                sem_frame_desde = None

                # Ajuste adaptativo: corrige luz/contraste conforme o ambiente detectado.
                # Retorna um novo array; detecção, bboxes e stream usam o frame corrigido.
                if self.ajustador.ativo:
                    frame = self.ajustador.processar(frame)

                # Guarda o frame LIMPO (sem bboxes/labels desenhados) para a leitura
                # manual (ler-placa) e snapshots. As caixas são desenhadas numa cópia,
                # senão o OCR do botão "Ler Placa" acaba lendo o próprio overlay.
                estado.registrar_frame_camera_limpo(self.camera_db_id, frame)

                frames_count += 1
                estado.incrementar_frame()
                agora = time.time()
                if agora - ultimo_fps >= 1.0:
                    estado.atualizar_fps(frames_count / (agora - ultimo_fps))
                    frames_count = 0
                    ultimo_fps = agora

                agora_det = time.time()
                frame_saida = frame
                if agora_det - self._ultima_deteccao >= self._intervalo_deteccao:
                    frame_saida = frame.copy()   # desenha bboxes aqui, preservando o limpo
                    self._processar_frame(frame_saida)
                    self._ultima_deteccao = agora_det
                estado.registrar_frame(frame_saida)
                estado.registrar_frame_camera(self.camera_db_id, frame_saida)

                # Trava a taxa do loop à frequência real da câmera.
                # Sem isso o loop spin a CPU a 100% relendo o mesmo frame cacheado.
                decorrido = time.time() - t_loop
                restante = _intervalo_loop - decorrido
                if restante > 0.001:
                    time.sleep(restante)

            except Exception as e:
                log.error("Pipeline câmera %d: exceção no loop: %s — retomando em 1s",
                          self.camera_db_id, e)
                time.sleep(1.0)

    def _processar_frame(self, frame) -> None:
        roi = self.roi
        if roi:
            rx, ry, rw, rh = roi["x"], roi["y"], roi["w"], roi["h"]
            frame_det = frame[ry:ry + rh, rx:rx + rw]
            if frame_det.size == 0:
                return
            bboxes_roi = self.detector.detectar(frame_det)
            bboxes = [(x + rx, y + ry, w, h, c) for x, y, w, h, c in bboxes_roi]
        else:
            bboxes = self.detector.detectar(frame)
        f_h, f_w = frame.shape[:2]
        if self.tracker is not None and self.tracker.ativo():
            self._processar_com_tracker(frame, bboxes, f_h, f_w)
        else:
            self._processar_classico(frame, bboxes, f_h, f_w)

    def _processar_com_tracker(self, frame, bboxes, f_h: int, f_w: int) -> None:
        tracks = self.tracker.update(bboxes, frame)
        for x, y, w, h, conf_det, track_id in tracks:
            x, y, w, h = _expandir_bbox(x, y, w, h, f_w, f_h)

            if self.tracker.precisa_ocr(track_id):
                crop = frame[y: y + h, x: x + w]
                if crop.size > 0:
                    texto, conf_ocr = self.ocr.ler(crop)
                    resultado = validar(texto)
                    if resultado:
                        placa, padrao = resultado
                        aceitar = True
                        if self.votos_minimos > 1 and hasattr(self.ocr, "_ultimo_detalhe"):
                            det = self.ocr._ultimo_detalhe
                            if det.get("votos", 1) < self.votos_minimos:
                                aceitar = False
                        if aceitar:
                            conf_total = (conf_det + conf_ocr) / 2
                            self.tracker.registrar_ocr(track_id, placa, padrao, conf_total)

            pronto = self.tracker.placa_pronta(track_id)
            if pronto:
                placa, padrao, conf = pronto
                self.tracker.marcar_emitido(track_id)
                self._desenhar_bbox(frame, x, y, w, h, placa, conf)
                self._emitir(placa, padrao, conf, frame, (x, y, w, h))
            else:
                votos = self.tracker.votos_atuais(track_id)
                self._desenhar_bbox_track(frame, x, y, w, h, track_id, conf_det, votos)

    def _processar_classico(self, frame, bboxes, f_h: int, f_w: int) -> None:
        """Comportamento original: OCR em todo bbox detectado, consenso por frames."""
        for x, y, w, h, conf_det in bboxes:
            x, y, w, h = _expandir_bbox(x, y, w, h, f_w, f_h)
            crop = frame[y: y + h, x: x + w]
            if crop.size == 0:
                continue
            texto, conf_ocr = self.ocr.ler(crop)
            resultado = validar(texto)
            if not resultado:
                self._desenhar_bbox_rejeitado(frame, x, y, w, h, texto, conf_det)
                log.debug("YOLO detectou (conf=%.2f) mas OCR/validador rejeitou: %r", conf_det, texto)
                continue
            if self.votos_minimos > 1 and hasattr(self.ocr, "_ultimo_detalhe"):
                det = self.ocr._ultimo_detalhe
                votos = det.get("votos", 1)
                total = det.get("total_engines", 1)
                if votos < self.votos_minimos:
                    log.debug("Placa %s rejeitada: %d/%d votos (mínimo %d)",
                              resultado[0], votos, total, self.votos_minimos)
                    self._desenhar_bbox_rejeitado(frame, x, y, w, h,
                                                  f"{resultado[0]}?{votos}/{total}", conf_det)
                    continue
            placa, padrao = resultado
            conf_total = (conf_det + conf_ocr) / 2
            self._desenhar_bbox(frame, x, y, w, h, placa, conf_total)
            self._tentar_emitir(placa, padrao, conf_total, frame, (x, y, w, h))

    def _desenhar_bbox(self, frame, x, y, w, h, placa, conf):
        cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)
        label = f"{placa} {conf:.2f}"
        cv2.putText(frame, label, (x, max(y - 8, 12)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

    def _desenhar_bbox_rejeitado(self, frame, x, y, w, h, texto_bruto, conf_det):
        cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 200, 255), 2)
        rotulo = (texto_bruto[:14] if texto_bruto else "(sem OCR)")
        label = f"? {rotulo} y={conf_det:.2f}"
        cv2.putText(frame, label, (x, max(y - 8, 12)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 200, 255), 2)

    def _desenhar_bbox_track(self, frame, x, y, w, h, track_id, conf_det, votos: int):
        """Bbox laranja com ID do track e votos OCR acumulados (ainda aguardando consenso)."""
        cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 165, 255), 2)
        label = f"ID:{track_id} {conf_det:.2f} [{votos}v]"
        cv2.putText(frame, label, (x, max(y - 8, 12)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 165, 255), 2)

    def _tentar_emitir(self, placa, padrao, conf, frame, bbox) -> None:
        """Modo clássico: acumula frames_consenso leituras iguais antes de emitir."""
        self._historico.append(placa)
        recentes = list(self._historico)[-self.frames_consenso:]
        if len(recentes) < self.frames_consenso or len(set(recentes)) != 1:
            return
        # Limpa histórico — evita segundo consenso imediato do mesmo veículo
        self._historico.clear()
        self._emitir(placa, padrao, conf, frame, bbox)

    def _emitir(self, placa, padrao, conf, frame, bbox) -> None:
        """Persiste detecção, atualiza estado, broadcast WS e dispara webhooks.
        Respeita cooldown_seg para evitar re-emissão do mesmo veículo parado.
        Usado por ambos os paths: tracker e clássico.
        """
        # Cooldown por SIMILARIDADE, não string exata: ruído de OCR de 1-2 caracteres
        # (0/O/D/Q, I/1/J...) fazia o mesmo veículo escapar do cooldown e virar uma
        # nova linha no histórico a cada leitura levemente diferente. Escopado por
        # câmera (camera_db_id) — duas câmeras diferentes não se afetam.
        recentes = estado.obter_emissoes_recentes(self.camera_db_id, self.cooldown_seg)
        casada = next((p for p, _ in recentes if parecidas(placa, p)), None)
        if casada is not None:
            estado.registrar_emissao(self.camera_db_id, casada)  # desliza o cooldown
            return

        # Além da própria câmera, cruza com leituras 'roteador'/'teste' já gravadas por
        # esta câmera: o mesmo veículo costuma ser lido tanto pelo monitoramento contínuo
        # quanto pela chamada reativa do bico quase ao mesmo tempo. A leitura reativa é o
        # evento com significado de negócio (ligada a um bico/abastecimento) — o pipeline
        # não precisa duplicar o que ela já registrou.
        desde = (datetime.now(timezone.utc) - timedelta(seconds=self.cooldown_seg)).isoformat()
        anterior = banco.ultima_deteccao_camera(self.camera_db_id, desde)
        if anterior and anterior["origem"] != "pipeline" and parecidas(placa, anterior["placa"]):
            estado.registrar_emissao(self.camera_db_id, placa)
            return

        estado.registrar_emissao(self.camera_db_id, placa)

        snapshot_rel = None
        if self.salvar_snapshot:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            nome = f"{ts}_{placa}.jpg"
            caminho = SNAPSHOT_DIR / nome
            x, y, w, h = bbox
            crop = frame[y: y + h, x: x + w]
            cv2.imwrite(str(caminho), crop, [int(cv2.IMWRITE_JPEG_QUALITY), self.snapshot_q])
            snapshot_rel = f"/static/snapshots/{nome}"

        deteccao_id = banco.registrar_deteccao(
            placa=placa,
            padrao=padrao,
            confianca=conf,
            snapshot=snapshot_rel,
            camera_id=self.cfg["camera_tipo"],
            bbox={"x": bbox[0], "y": bbox[1], "w": bbox[2], "h": bbox[3]},
            origem="pipeline",
            camera_db_id=self.camera_db_id,
        )
        criado_em = datetime.now(timezone.utc).isoformat()
        estado.adicionar_deteccao({
            "id": deteccao_id, "placa": placa, "padrao": padrao,
            "confianca": conf, "snapshot": snapshot_rel, "criado_em": criado_em,
        })

        entrada_lista = banco.listas_buscar(placa)
        lista = entrada_lista["tipo"] if entrada_lista else None
        bc.broadcaster.push({
            "tipo": "deteccao", "placa": placa, "padrao": padrao,
            "confianca": round(conf, 3), "snapshot": snapshot_rel,
            "criado_em": criado_em, "bomba": self.bomba, "lado": self.lado,
            "lista": lista,
        })

        log.info("Placa detectada: %s (%s, conf=%.2f)", placa, padrao, conf)
        self._notificar_webhook_todas(placa, padrao, conf, snapshot_rel)
        self._verificar_alerta(placa, padrao)

    def _notificar_webhook_todas(self, placa: str, padrao: str, conf: float, snapshot: str | None) -> None:
        """Webhook para TODA detecção — disparado em thread separada para não bloquear o pipeline."""
        if self.cfg.get("webhook_todas", "").strip().lower() not in ("sim", "true", "1"):
            return
        url = self.cfg.get("webhook_url", "").strip()
        if not url:
            log.warning("webhook_todas=sim mas webhook_url vazio — pulei notificação de %s", placa)
            return
        payload = {"bomba": self.bomba, "lado": self.lado, "placa": placa,
                   "padrao": padrao, "confianca": round(conf, 3), "snapshot": snapshot}
        log.info("[ECHO] bomba=%s lado=%s placa=%s", self.bomba, self.lado, placa)
        threading.Thread(
            target=self._enviar_webhook, args=(url, payload, "webhook"),
            daemon=True, name="alpr-webhook",
        ).start()

    def _verificar_alerta(self, placa: str, padrao: str) -> None:
        if not self.cfg.get("alerta_lista_negra", "").lower() in ("sim", "true", "1"):
            return
        entrada = banco.listas_buscar(placa)
        if not entrada or entrada["tipo"] != "negra":
            return
        url = self.cfg.get("webhook_url", "").strip()
        if not url:
            log.warning("ALERTA: placa NEGRA detectada (%s) — webhook não configurado", placa)
            return
        payload = {"placa": placa, "padrao": padrao, "descricao": entrada.get("descricao", ""),
                   "alerta": "lista_negra"}
        threading.Thread(
            target=self._enviar_webhook, args=(url, payload, "alerta lista negra"),
            daemon=True, name="alpr-webhook-alerta",
        ).start()

    @staticmethod
    def _enviar_webhook(url: str, payload: dict, label: str) -> None:
        try:
            import requests
            requests.post(url, json=payload, timeout=5)
            log.info("Webhook '%s' enviado: %s", label, payload.get("placa"))
        except Exception as e:
            log.error("Falha no webhook '%s': %s", label, e)


_instancias: dict[int, Pipeline] = {}


def _cfg_para_camera(global_cfg: dict, cam: dict) -> dict:
    merged = dict(global_cfg)
    for k in ("camera_tipo", "camera_indice", "intelbras_host", "intelbras_porta",
              "intelbras_usuario", "intelbras_canal",
              "intelbras_subtype", "intelbras_formato"):
        merged[k] = str(cam.get(k, merged.get(k, "")))
    # Senha: só sobrescreve se estiver preenchida no banco; senão usa a do config global
    if cam.get("intelbras_senha"):
        merged["intelbras_senha"] = str(cam["intelbras_senha"])
    # URL personalizada: se preenchida, usa direto (ignora host/canal/formato)
    if cam.get("rtsp_url_custom"):
        merged["camera_indice"] = str(cam["rtsp_url_custom"])
        merged["intelbras_host"] = ""   # força uso de camera_indice em _origem_rtsp
    # Identificação do ponto de abastecimento
    merged["bomba"] = str(cam.get("bomba", "0"))
    merged["lado"] = str(cam.get("lado", "0"))
    if cam.get("roi"):
        merged["roi"] = cam["roi"]   # JSON string da área de captura
    else:
        merged.pop("roi", None)
    return merged


def iniciar_camera(camera_db_id: int, cfg: dict[str, str]) -> None:
    p = Pipeline(cfg, camera_db_id=camera_db_id)
    # Registra ANTES de iniciar: `iniciar()` já abre o RTSP e demora dezenas de segundos
    # (conexão + carga dos modelos). Nessa janela a câmera está ocupada, e quem consultar
    # `_instancias` precisa saber disso — senão tenta uma segunda conexão que vai falhar,
    # porque a câmera aceita só uma.
    _instancias[camera_db_id] = p
    try:
        p.iniciar()
    except Exception:
        # NÃO remove de `_instancias` (diferente de antes): sem thread viva e com
        # `iniciando=False`, a instância cai no mesmo caminho de "thread morta" que o
        # supervisor (app/operacao/supervisor.py) já trata com backoff exponencial.
        # Removê-la fazia essa câmera desaparecer de vez do supervisor após uma falha
        # no boot (ex.: modelo não carregou) — sem retry algum, exigindo intervenção
        # manual pra sempre recuperar.
        p.iniciando = False
        raise


def iniciar_cameras_db(cfg: dict[str, str]) -> None:
    """Inicia um pipeline por câmera ativa no banco. Sem câmeras cadastradas, não inicia nada."""
    cameras_ativas = [c for c in banco.cameras_listar() if c["ativo"]]
    if not cameras_ativas:
        log.info("Nenhuma câmera cadastrada — pipeline aguarda cadastro via interface")
        return
    for cam in cameras_ativas:
        try:
            iniciar_camera(cam["id"], _cfg_para_camera(cfg, cam))
        except Exception as e:
            log.error("Falha ao iniciar pipeline câmera %s: %s", cam.get("nome"), e)


def iniciar(cfg: dict[str, str]) -> None:
    iniciar_cameras_db(cfg)


def parar_camera(camera_db_id: int) -> None:
    p = _instancias.pop(camera_db_id, None)
    if p is not None:
        p.parar()
        with estado.lock:
            estado.frames_cameras.pop(camera_db_id, None)
            estado.frames_cameras_limpos.pop(camera_db_id, None)


def reiniciar_camera(camera_db_id: int, cfg: dict[str, str]) -> None:
    parar_camera(camera_db_id)
    iniciar_camera(camera_db_id, cfg)


def parar_todas() -> None:
    for p in list(_instancias.values()):
        p.parar()
    _instancias.clear()
    with estado.lock:
        estado.frames_cameras.clear()
        estado.frames_cameras_limpos.clear()


def parar() -> None:
    parar_todas()


def reiniciar(cfg: dict[str, str]) -> None:
    """Para todas as instâncias e reinicia com a nova config."""
    parar_todas()
    iniciar_cameras_db(cfg)
