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
from app.visao import contexto_log
from app.visao.camera import Camera
from app.visao.consenso import confirmada as _confirmada
from app.visao.detector import Detector, MultiDetector, OrigemTipo, deslocar, origem_de_bbox
from app.visao.ocr import OCR
from app.visao.ocr.auto import crop_legivel
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


# IoU mínimo para dar a uma caixa de track o tipo de uma detecção deste frame. Com o
# `_IoUTracker` o casamento é exato (IoU 1,0); o piso existe para o ByteTrack, cuja caixa
# passa pelo filtro de Kalman e sai levemente deslocada da detecção que a alimentou.
IOU_MIN_TRACK = 0.5


def _origem_do_track(bbox_track: tuple, bboxes) -> OrigemTipo | None:
    """Origem do tipo de veículo (classe + sinal cru) da detecção que melhor casa com
    esta caixa de track.

    None quando nenhuma detecção deste frame se sobrepõe o bastante — o track pode estar
    sendo predito sem detecção nova (paciência do tracker), e nesse caso não há veículo
    observado agora para afirmar o tipo. Devolve o objeto inteiro, e não só o `str` do
    tipo: os quatro campos (`tipo_veiculo`/`veiculo_classe`/`veiculo_conf`/
    `tipo_veiculo_fonte`) têm que gravar juntos, e um objeto é o que torna "mover em
    bloco" o caminho natural.
    """
    melhor, melhor_iou = None, 0.0
    for b in bboxes:
        iou = MultiDetector._iou(bbox_track, b)
        if iou > melhor_iou:
            melhor, melhor_iou = b, iou
    return origem_de_bbox(melhor) if melhor_iou >= IOU_MIN_TRACK else None


def _vale_como_negativo(crop) -> bool:
    """Se este recorte não-lido merece virar imagem na fila de classificação.

    Um negativo serve para alguém olhar e dizer qual era a placa. Recorte abaixo do
    mínimo do OCR não tem o que olhar: medido em 13/08/2026, 38,5% dos 936 negativos já
    coletados estão nessa faixa, e não há como rotulá-los. Eles consumiam a cota de
    `captura_dataset_max_arquivos` (5000) que existe para guardar o caso difícil — e,
    quando a cota estoura, a coleta PARA, inclusive para os negativos que valem.
    """
    if crop is None or crop.size == 0 or crop.ndim != 3:
        return False
    h, w = crop.shape[:2]
    return crop_legivel(w, h)


def _maxlen_historico(frames_consenso: int) -> int:
    """Tamanho mínimo do deque `_historico` para que `frames_consenso` sempre caiba.

    A UI de config permite `frames_consenso` de 1 a 20 sem validação server-side. Um
    `maxlen` menor que `frames_consenso` faz `_tentar_emitir` descartar leituras
    ANTES de `recentes` alcançar `frames_consenso` itens — a emissão trava em
    silêncio (sem log de erro), embora o bbox verde continue sendo desenhado como se
    a leitura estivesse indo pro ar. 10 é o piso histórico (comportamento inalterado
    para configs <=10).
    """
    return max(frames_consenso, 10)


def _consenso_janela(janela: list[str], placa: str) -> tuple[float, int]:
    """(acordo, votos) da placa emitida dentro da janela de leituras válidas recentes.

    É o equivalente, no modo clássico, do que o tracker calcula por veículo: que fração
    das leituras recentes apontou a placa que foi emitida. A janela é o `_historico`, que
    guarda só o que PASSOU pelo validador e é zerado a cada emissão — o que o OCR cuspiu
    e o validador recusou nunca entra aqui, então o denominador são leituras plausíveis,
    não lixo.

    Esta conta pode SUBESTIMAR: sem tracker não existe noção de veículo, e se o carro
    anterior foi lido mas nunca fechou consenso, as leituras dele continuam na janela e
    entram no denominador do carro seguinte. Isso é de propósito, e o erro está no lado
    seguro: subestimar só produz um selo "a conferir" a mais no histórico (alguém olha
    uma imagem à toa), enquanto superestimar esconde uma leitura fraca e ela vira cobrança
    sem ninguém olhar. Quem quiser a medida exata por veículo liga o tracker, que é o
    padrão — este caminho é o fallback de quando ele está desligado.
    """
    if not janela:
        return 0.0, 0
    votos = janela.count(placa)
    return votos / len(janela), votos


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
        # Coleta para o dataset de testes — separada da gravação do histórico, que só
        # registra leitura bem-sucedida e por isso nunca captura o que falha.
        from app.visao.captura_dataset import CapturaDataset
        self.captura_dataset = CapturaDataset(cfg, self.camera_db_id)
        self.votos_minimos = max(1, int(cfg.get("ocr_votos_minimos", "1")))
        self.frames_consenso = int(cfg["frames_consenso"])
        # Mesmo limiar da leitura reativa, de propósito: as duas origens caem na MESMA
        # tabela e no mesmo histórico, e um "a conferir" que significasse coisas
        # diferentes conforme a linha não serviria para decidir nada.
        self.acordo_min = float(cfg.get("leitura_acordo_minimo", "0.80"))
        self.cooldown_seg = int(cfg["cooldown_seg"])
        self._intervalo_deteccao = 1.0 / max(1, int(cfg.get("deteccao_fps_max", "5")))
        self.salvar_snapshot = cfg["salvar_snapshot"].lower() in ("sim", "true", "1")
        # Mesma chave que a leitura reativa usa (app/visao/leitura.py): o histórico
        # mostra as duas origens na mesma tabela, e uma linha do pipeline sem o quadro
        # era indistinguível de uma leitura antiga, anterior à gravação do contexto.
        self.salvar_frame = cfg.get("salvar_frame_deteccao", "sim").lower() in ("sim", "true", "1")
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

        self._historico: deque = deque(maxlen=_maxlen_historico(self.frames_consenso))
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

    def parar(self) -> bool:
        """Sinaliza a thread do loop para parar e, só depois que ela morrer de
        verdade, fecha a câmera. Retorna False (SEM fechar a câmera) se a thread
        não morrer dentro do timeout — o chamador NÃO pode prosseguir para reabrir
        a câmera nesse caso.

        Fechar a câmera de outra thread enquanto `_loop_camera` ainda está viva não é
        seguro: a thread do loop pode estar no meio de `self.camera.reconectar()`
        (que abre uma NOVA conexão RTSP) ou prestes a chamar `self.camera.ler()` logo
        depois de `fechar()` ter zerado o `cap`. O efeito prático que motivou esta
        correção é o de `reiniciar_camera`: se ele seguisse em frente mesmo com a
        thread antiga viva, `iniciar_camera` abriria uma SEGUNDA conexão RTSP
        concorrente para a mesma câmera física — que a Intelbras (e a maioria das
        câmeras IP) só aceita uma.
        """
        self._parar.set()
        if self._thread:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                log.error(
                    "Câmera %d: thread do pipeline não encerrou em 5s (provável "
                    "travada em leitura/reconexão de câmera) — NÃO fechando a câmera "
                    "nem prosseguindo, para não abrir uma segunda conexão RTSP "
                    "concorrente enquanto a thread antiga ainda está viva",
                    self.camera_db_id,
                )
                return False
        self.camera.fechar()
        estado.pipeline_rodando = False
        estado.camera_conectada = False
        log.info("Pipeline parado")
        return True

    def _loop(self) -> None:
        # Rotula TUDO que esta thread logar com a câmera de origem — inclusive o que sai
        # lá do fundo do OCR, que não tem como saber de onde veio o recorte. Sem isso as
        # linhas de dois pipelines se intercalam sem dono; ver app/visao/contexto_log.py.
        with contexto_log.usar(camera=self.camera_db_id):
            self._loop_camera()

    def _loop_camera(self) -> None:
        if not self.deteccao_automatica:
            while not self._parar.is_set():
                time.sleep(1.0)
            return

        ultimo_fps = time.time()
        frames_count = 0
        sem_frame_desde: float | None = None
        _intervalo_loop = 1.0 / max(1, int(self.cfg.get("camera_fps", "15")))

        # Estado do tick de detecção/publicação (cadência de `deteccao_fps_max`,
        # tipicamente mais lenta que a da câmera). `ultimo_bruto` é o frame CRU (antes
        # do ajuste) já processado no tick anterior — comparado por IDENTIDADE, não
        # conteúdo: `Camera.ler()` devolve sempre o MESMO objeto até a thread leitora
        # entregar um frame novo (app/visao/camera.py), então "mesmo objeto" É "câmera
        # não trouxe nada novo entre os dois ticks". `ultimo_saida` é o último frame
        # publicado (com bboxes, se houve detecção).
        ultimo_bruto = None
        ultimo_saida = None

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
                            # Fatiado em passos de 1s em vez de um único sleep(30): sem
                            # isso, `parar()` (Correção 3) podia esperar até 30s pela
                            # thread notar `_parar` antes de decidir se é seguro fechar
                            # a câmera. Checar o flag a cada segundo deixa o encerramento
                            # rápido no caso comum, sem mudar a cadência de retry.
                            for _ in range(30):
                                if self._parar.is_set():
                                    break
                                time.sleep(1)
                    else:
                        time.sleep(0.1)
                    # A câmera sumiu: nenhuma garantia sobre o próximo frame que vier
                    # (pode ser de uma reconexão). Zera as marcas para não arriscar
                    # casar por identidade com o frame de antes da queda.
                    ultimo_bruto = ultimo_saida = None
                    continue

                sem_frame_desde = None

                # FPS/contador de frames continuam na cadência de CAPTURA (o painel
                # mostra ~camera_fps, não a taxa de detecção — comportamento inalterado).
                frames_count += 1
                estado.incrementar_frame()
                agora = time.time()
                if agora - ultimo_fps >= 1.0:
                    estado.atualizar_fps(frames_count / (agora - ultimo_fps))
                    frames_count = 0
                    ultimo_fps = agora

                # ── Ajuste + publicação + detecção, juntos, na cadência de DETECÇÃO ──
                # Não dá pra publicar o frame ajustado numa cadência e detectar noutra:
                # app/visao/leitura.py (ler_placa) NÃO reaplica o ajuste quando usa o
                # frame publicado aqui — confia que já vem ajustado. Publicar um frame
                # cru por engano faria o OCR do endpoint de produção (GET /api/leitura)
                # degradar em silêncio, e o stream/HLS piscaria brilho/cor a cada frame
                # não-ajustado. Por isso os três — ajuste, publicação e detecção — só
                # acontecem juntos, no mesmo portão, e nada é publicado entre ticks.
                if agora - self._ultima_deteccao >= self._intervalo_deteccao:
                    self._ultima_deteccao = agora

                    if frame is ultimo_bruto and ultimo_saida is not None:
                        # Câmera não entregou frame novo desde o último tick (comum
                        # quando ela é mais lenta que `camera_fps`, ou reconectando).
                        # Reprocessar (ajuste + YOLO + OCR) sobre o MESMO array daria
                        # byte a byte o mesmo resultado — pula o trabalho pesado, mas
                        # REPUBLICA, pra `ultimo_frame_ts` continuar andando exatamente
                        # como antes: os checks de frescor (app/web/leitura.py,
                        # supervisor) não podem enxergar diferença entre "câmera
                        # parada" e "tick sem novidade".
                        estado.registrar_frame(ultimo_saida)
                        estado.registrar_frame_camera(self.camera_db_id, ultimo_saida)
                    else:
                        ultimo_bruto = frame
                        if self.ajustador.ativo:
                            frame = self.ajustador.processar(frame)

                        # Frame LIMPO (sem bboxes/labels) para leitura manual e
                        # snapshots. As caixas são desenhadas numa CÓPIA, senão o OCR
                        # do botão "Ler Placa" acaba lendo o próprio overlay.
                        estado.registrar_frame_camera_limpo(self.camera_db_id, frame)

                        frame_saida = frame.copy()
                        self._processar_frame(frame_saida, frame)
                        ultimo_saida = frame_saida
                        estado.registrar_frame(frame_saida)
                        estado.registrar_frame_camera(self.camera_db_id, frame_saida)

                # Trava a taxa do loop à frequência real da câmera — a detecção de
                # câmera morta e o contador de fps dependem de rodar nessa cadência.
                decorrido = time.time() - t_loop
                restante = _intervalo_loop - decorrido
                if restante > 0.001:
                    time.sleep(restante)

            except Exception as e:
                log.error("Pipeline câmera %d: exceção no loop: %s — retomando em 1s",
                          self.camera_db_id, e)
                time.sleep(1.0)

    def _processar_frame(self, frame, frame_limpo) -> None:
        """`frame` é a cópia que vai virar o MJPEG e RECEBE os retângulos; `frame_limpo`
        é o quadro como veio da câmera. Os dois andam juntos porque o que é bom para o
        stream (caixa e rótulo por cima) é exatamente o que estraga o que vai para o
        disco: um recorte com o retângulo desenhado em cima da placa não serve para
        auditar leitura nem para virar dado de treino."""
        roi = self.roi
        if roi:
            rx, ry, rw, rh = roi["x"], roi["y"], roi["w"], roi["h"]
            frame_det = frame[ry:ry + rh, rx:rx + rw]
            if frame_det.size == 0:
                return
            bboxes_roi = self.detector.detectar(frame_det)
            # `deslocar` e não uma comprehension crua: remontar a tupla descartaria o
            # `tipo_veiculo` que o 2 estágios anexou. Ramo dormente hoje (a tabela
            # `cameras` não tem coluna `roi`, então `_cfg_para_camera` deixa self.roi
            # None), mas o dia em que uma ROI de câmera existir o tipo sumiria calado.
            bboxes = [deslocar(b, rx, ry) for b in bboxes_roi]
        else:
            bboxes = self.detector.detectar(frame)
        # Amostra o quadro INTEIRO, tenha havido detecção ou não. É o único gatilho que
        # pega moto cuja placa nem chega a ser detectada — e essa é a hipótese mais
        # provável hoje, já que a varredura em janelas que resolveu moto está no caminho
        # da leitura GET, não neste pipeline. Vai o quadro LIMPO: antes ia o mesmo
        # array que recebe os retângulos, sem problema só porque o desenho ainda não
        # tinha acontecido — uma dependência de ordem que não sobrevive à primeira
        # reorganização deste método, e o dataset é o último lugar onde se quer
        # descobrir que a imagem tem o palpite do sistema desenhado por cima.
        self.captura_dataset.amostrar(frame_limpo)

        f_h, f_w = frame.shape[:2]
        if self.tracker is not None and self.tracker.ativo():
            self._processar_com_tracker(frame, bboxes, f_h, f_w, frame_limpo)
        else:
            self._processar_classico(frame, bboxes, f_h, f_w, frame_limpo)

    def _processar_com_tracker(self, frame, bboxes, f_h: int, f_w: int, frame_limpo) -> None:
        tracks = self.tracker.update(bboxes, frame)
        for x, y, w, h, conf_det, track_id in tracks:
            # O tracker devolve tuplas próprias, então a origem da bbox original não
            # sobrevive à volta — recupera-se pela caixa de maior IoU. Com o
            # `_IoUTracker` (o backend em uso quando boxmot não está instalado) a caixa do
            # track É a da detecção e o casamento é exato; o IoU existe para o ByteTrack,
            # que suaviza a caixa por Kalman. Sem match suficiente → None, nunca um chute.
            origem_tipo = _origem_do_track((x, y, w, h), bboxes)
            x, y, w, h = _expandir_bbox(x, y, w, h, f_w, f_h)
            with contexto_log.usar(track=track_id):
                self._processar_track(frame, frame_limpo, x, y, w, h, conf_det, track_id,
                                      origem_tipo=origem_tipo)

    def _processar_track(self, frame, frame_limpo, x, y, w, h,
                         conf_det: float, track_id: int,
                         origem_tipo: OrigemTipo | None = None) -> None:
        """Um veículo rastreado, num tick. Extraído do laço para que o contexto de log
        (`[camN trkN]`) tenha um bloco onde valer."""
        if self.tracker.precisa_ocr(track_id):
            # Do quadro limpo: este recorte vai para o OCR e, quando a leitura
            # falha, para o dataset — nos dois casos o retângulo de uma detecção
            # anterior atravessando a placa é ruído introduzido pelo próprio sistema.
            crop = frame_limpo[y: y + h, x: x + w]
            if crop.size > 0:
                texto, conf_ocr = self.ocr.ler(crop)
                resultado = validar(texto)
                if not resultado and _vale_como_negativo(crop):
                    # Mesmo caso do modo clássico: detectou e não leu. Sem isto, o
                    # gatilho de negativo só existiria em um dos dois modos.
                    self.captura_dataset.negativo(crop)
                if resultado:
                    placa, padrao = resultado
                    aceitar = True
                    if self.votos_minimos > 1 and hasattr(self.ocr, "_ultimo_detalhe"):
                        det = self.ocr._ultimo_detalhe
                        if det.get("votos", 1) < self.votos_minimos:
                            aceitar = False
                            log.info("Leitura %s descartada: %d de %d engines (mínimo %d)",
                                     placa, det.get("votos", 1),
                                     det.get("total_engines", 1), self.votos_minimos)
                    if aceitar:
                        conf_total = (conf_det + conf_ocr) / 2
                        self.tracker.registrar_ocr(track_id, placa, padrao, conf_total)

        pronto = self.tracker.placa_pronta(track_id)
        if pronto:
            placa, padrao, conf = pronto
            # Quantas das leituras deste veículo apontaram a placa emitida. É a
            # medida de consenso do contínuo, e sem ela a detecção chegava ao banco
            # com `acordo`/`confirmada` nulos — indistinguível, no histórico, de uma
            # leitura sólida.
            votos, total = self.tracker.consenso(track_id)
            acordo = votos / total if total else 0.0
            self.tracker.marcar_emitido(track_id)
            self._desenhar_bbox(frame, x, y, w, h, placa, conf)
            self._emitir(placa, padrao, conf, frame_limpo, (x, y, w, h),
                         acordo=acordo,
                         confirmada=_confirmada(acordo, votos, self.acordo_min,
                                                self.tracker.votos_minimos),
                         votos=votos, total_leituras=total,
                         origem_tipo=origem_tipo)
        else:
            votos = self.tracker.votos_atuais(track_id)
            self._desenhar_bbox_track(frame, x, y, w, h, track_id, conf_det, votos)

    def _processar_classico(self, frame, bboxes, f_h: int, f_w: int, frame_limpo) -> None:
        """Comportamento original: OCR em todo bbox detectado, consenso por frames."""
        for bb in bboxes:
            x, y, w, h, conf_det = bb
            # Antes de `_expandir_bbox`, que devolve tupla crua e perderia a origem.
            origem_tipo = origem_de_bbox(bb)
            x, y, w, h = _expandir_bbox(x, y, w, h, f_w, f_h)
            crop = frame_limpo[y: y + h, x: x + w]   # mesmo motivo do modo tracker
            if crop.size == 0:
                continue
            texto, conf_ocr = self.ocr.ler(crop)
            resultado = validar(texto)
            if not resultado:
                self._desenhar_bbox_rejeitado(frame, x, y, w, h, texto, conf_det)
                log.debug("YOLO detectou (conf=%.2f) mas OCR/validador rejeitou: %r", conf_det, texto)
                # Achou a placa e não conseguiu ler: é justamente o caso que o dataset
                # não tem, porque o histórico só guarda o que deu certo.
                if _vale_como_negativo(crop):
                    self.captura_dataset.negativo(crop)
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
            self._tentar_emitir(placa, padrao, conf_total, frame_limpo, (x, y, w, h),
                                origem_tipo=origem_tipo)

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

    def _tentar_emitir(self, placa, padrao, conf, frame_limpo, bbox,
                       origem_tipo: OrigemTipo | None = None) -> None:
        """Modo clássico: acumula frames_consenso leituras iguais antes de emitir."""
        self._historico.append(placa)
        recentes = list(self._historico)[-self.frames_consenso:]
        if len(recentes) < self.frames_consenso or len(set(recentes)) != 1:
            return
        # Medido ANTES do clear, que é o que apaga a janela.
        total_janela = len(self._historico)
        acordo, votos = _consenso_janela(list(self._historico), placa)
        # Limpa histórico — evita segundo consenso imediato do mesmo veículo
        self._historico.clear()
        self._emitir(placa, padrao, conf, frame_limpo, bbox, acordo=acordo,
                     confirmada=_confirmada(acordo, votos, self.acordo_min,
                                            self.frames_consenso),
                     votos=votos, total_leituras=total_janela,
                     origem_tipo=origem_tipo)

    def _emitir(self, placa, padrao, conf, frame_limpo, bbox,
                acordo: float, confirmada: bool,
                votos: int = 0, total_leituras: int = 0,
                origem_tipo: OrigemTipo | None = None) -> None:
        """Persiste detecção, atualiza estado, broadcast WS e dispara webhooks.
        Respeita cooldown_seg para evitar re-emissão do mesmo veículo parado.
        Usado por ambos os paths: tracker e clássico.

        `acordo`/`confirmada` chegam prontos porque só quem chama sabe o que conta como
        um voto no seu modo (leituras do mesmo veículo rastreado, ou frames consecutivos)
        — obrigatórios de propósito, para que um terceiro modo de emissão no futuro não
        consiga gravar detecção sem declarar quão sólida ela é.

        `votos`/`total_leituras` são só para o log: `acordo=0.10` responde "quão sólida",
        mas `2/20` responde "sólida com base em quê", e é a diferença entre uma placa
        confirmada duas vezes e um veículo que foi lido vinte vezes sem nunca convergir.

        `origem_tipo` carrega o tipo do veículo (vindo da bbox — classe do detector de
        veículo) E o sinal cru por trás dele: os quatro campos que ele expande
        (`tipo_veiculo`/`veiculo_classe`/`veiculo_conf`/`tipo_veiculo_fonte`) sempre
        gravam juntos. Tem default ao contrário de `acordo`/`confirmada`: ausência aqui é
        um estado legítimo e frequente (2 estágios desligado, nenhum veículo detectado,
        placa vinda das janelas), e tem que ser barata de expressar. O "esqueci de passar"
        é pego pelos testes de propagação dos dois modos, não pela assinatura.
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

        x, y, w, h = bbox
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

        # Recorte do quadro LIMPO. Vinha do quadro do stream, que já tinha recebido o
        # retângulo verde e o rótulo do `_desenhar_bbox` logo antes desta chamada — ou
        # seja, todo recorte gravado pelo contínuo saía com uma caixa desenhada em cima
        # da placa. Dá para ver isso nas miniaturas antigas do histórico.
        snapshot_rel = None
        if self.salvar_snapshot:
            nome = f"{ts}_{placa}.jpg"
            caminho = SNAPSHOT_DIR / nome
            crop = frame_limpo[y: y + h, x: x + w]
            cv2.imwrite(str(caminho), crop, [int(cv2.IMWRITE_JPEG_QUALITY), self.snapshot_q])
            snapshot_rel = f"/static/snapshots/{nome}"

        # Quadro inteiro com a caixa lida, igual ao que a leitura reativa grava. Só o
        # recorte não permite auditar erro do contínuo: numa placa lida errada não dá
        # para saber se o detector pegou o veículo da pista ao lado, um adesivo ou um
        # reflexo — a informação que responde isso só existe fora do recorte.
        # A marcação é desenhada AQUI, sobre o quadro limpo: usar o quadro do stream
        # (que já vem anotado) escrevia placa e caixa duas vezes, uma por cima da outra.
        frame_rel = None
        if self.salvar_frame:
            nome_f = f"{ts}_{placa}_frame.jpg"
            marcado = frame_limpo.copy()
            cv2.rectangle(marcado, (x, y), (x + w, y + h), (0, 200, 255), 2)
            cv2.putText(marcado, placa, (x, max(y - 8, 14)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 255), 2)
            if self.roi:
                r = self.roi
                cv2.rectangle(marcado, (r["x"], r["y"]),
                              (r["x"] + r["w"], r["y"] + r["h"]), (120, 120, 120), 1)
            cv2.imwrite(str(SNAPSHOT_DIR / nome_f), marcado,
                        [int(cv2.IMWRITE_JPEG_QUALITY), self.snapshot_q])
            frame_rel = f"/static/snapshots/{nome_f}"

        # Desempacota os quatro campos aqui — o único ponto em que `banco` (que não deve
        # conhecer tipos de `visao`) precisa deles como kwargs soltos.
        tipo_veiculo = origem_tipo.tipo if origem_tipo else None
        veiculo_classe = origem_tipo.classe if origem_tipo else None
        veiculo_conf = origem_tipo.conf if origem_tipo else None
        tipo_veiculo_fonte = origem_tipo.fonte if origem_tipo else None

        deteccao_id = banco.registrar_deteccao(
            placa=placa,
            padrao=padrao,
            confianca=conf,
            snapshot=snapshot_rel,
            camera_id=self.cfg["camera_tipo"],
            bbox={"x": bbox[0], "y": bbox[1], "w": bbox[2], "h": bbox[3]},
            frame=frame_rel,
            origem="pipeline",
            camera_db_id=self.camera_db_id,
            acordo=round(acordo, 3),
            confirmada=confirmada,
            # Classe do detector de veículo (YOLOX), carregada pela própria bbox desde
            # `DetectorDoisEstagios`. Antes vinha de `self.ocr._ultimo_tipo_veiculo`, que
            # tinha DOIS defeitos: a fonte era o aspecto do recorte da placa (o limiar
            # 2,0 cai no percentil 30 de uma população quase toda de carro), e o atributo
            # é um estado compartilhado lido fora do escopo do crop — num quadro com dois
            # veículos, ou num tick sem OCR novo, gravava o tipo do veículo errado.
            tipo_veiculo=tipo_veiculo,
            veiculo_classe=veiculo_classe, veiculo_conf=veiculo_conf,
            tipo_veiculo_fonte=tipo_veiculo_fonte,
        )
        criado_em = datetime.now(timezone.utc).isoformat()
        estado.adicionar_deteccao({
            "id": deteccao_id, "placa": placa, "padrao": padrao,
            "confianca": conf, "snapshot": snapshot_rel, "criado_em": criado_em,
            # Sem isto o painel de recentes e o WS mostravam tipo vazio para tudo que vem
            # do contínuo, enquanto a leitura reativa já mandava o campo.
            "tipo_veiculo": tipo_veiculo,
        })

        entrada_lista = banco.listas_buscar(placa)
        lista = entrada_lista["tipo"] if entrada_lista else None
        bc.broadcaster.push({
            "tipo": "deteccao", "placa": placa, "padrao": padrao,
            "confianca": round(conf, 3), "snapshot": snapshot_rel,
            "criado_em": criado_em, "bomba": self.bomba, "lado": self.lado,
            "lista": lista, "tipo_veiculo": tipo_veiculo,
        })

        log.info("EMITIDA %s (%s, conf=%.2f, acordo=%.2f%s, tipo=%s)%s", placa, padrao, conf, acordo,
                 " → %d/%d leituras" % (votos, total_leituras) if total_leituras else "",
                 tipo_veiculo or "nao-estimado",
                 "" if confirmada else " NAO-CONFIRMADA")
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

# Idade máxima do último frame publicado para a câmera ainda contar como "ao vivo".
# Folgado de propósito: o pipeline publica a `deteccao_fps_max` (5/s = 200ms), então
# 5s só é ultrapassado quando a transmissão realmente parou.
FRAME_VIVO_MAX_IDADE_SEG = 5.0


def estado_stream(camera_db_id: int) -> str:
    """Como a interface deve buscar a imagem desta câmera.

    - "ao_vivo":    pipeline contínuo publicando frames → MJPEG em /stream/{id}.mjpg
    - "aquecendo":  pipeline contínuo segurando a câmera mas ainda sem frame recente
                    (subindo, ou reconectando) → MJPEG assim que o primeiro frame sair.
                    NÃO é sob demanda: a Intelbras aceita uma conexão RTSP só, e ela
                    está com o pipeline — uma captura direta agora falharia.
    - "sob_demanda": ninguém está com a câmera → captura direta (1 frame e desconecta)

    Existir em `_instancias` NÃO significa que há imagem saindo: com
    `deteccao_automatica=nao` o pipeline nem abre a câmera (`_loop` só dorme), e
    `iniciar_camera` deixa a instância registrada mesmo quando `iniciar()` falha.
    A tela usava justamente `id in _instancias` como "ao vivo" e apontava o <img>
    para um MJPEG que nunca emitia byte nenhum — retângulo vazio, sem erro nenhum.
    """
    p = _instancias.get(camera_db_id)
    if p is None or not p.deteccao_automatica:
        return "sob_demanda"
    idade = time.time() - estado.ultimo_frame_ts.get(camera_db_id, 0.0)
    if idade <= FRAME_VIVO_MAX_IDADE_SEG:
        return "ao_vivo"
    if p.iniciando or (p._thread is not None and p._thread.is_alive()):
        return "aquecendo"
    # Thread morta e sem frame: o supervisor vai reiniciar com backoff. Até lá, a
    # captura direta é a única chance de imagem — pode falhar (se a conexão RTSP
    # anterior ficou pendurada), e aí a tela mostra o erro da captura, que é
    # informação de verdade em vez de um espaço em branco.
    return "sob_demanda"


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


def parar_camera(camera_db_id: int) -> bool:
    """Para e desregistra o pipeline desta câmera. Retorna False, SEM desregistrar
    nada, se a thread do pipeline não confirmou parada (ver `Pipeline.parar()`) —
    quem chama não pode tratar a câmera como livre nesse caso."""
    p = _instancias.get(camera_db_id)
    if p is None:
        return True
    if not p.parar():
        # NÃO faz `_instancias.pop(...)` aqui (diferente de antes): a thread zumbi
        # ainda está viva e pode estar usando `self.camera` — remover a instância
        # faria `estado_stream()` (e uma nova chamada a `iniciar_camera`) achar a
        # câmera livre e abrir uma segunda conexão RTSP concorrente com a antiga.
        return False
    _instancias.pop(camera_db_id, None)
    with estado.lock:
        estado.frames_cameras.pop(camera_db_id, None)
        estado.frames_cameras_limpos.pop(camera_db_id, None)
    # Sem isto o cache de JPEG do stream (app/streaming/stream.py) mantinha vivo
    # o último frame desta câmera indefinidamente, e podia servir imagem velha a
    # um viewer que reconectasse antes do primeiro frame novo pós-reinício.
    from app.streaming import stream as stream_mod
    stream_mod.descartar_cache(camera_db_id)
    return True


def reiniciar_camera(camera_db_id: int, cfg: dict[str, str]) -> bool:
    """Para o pipeline atual e sobe um novo com `cfg`. Retorna False SEM tentar abrir
    uma nova conexão se `parar_camera` não conseguiu confirmar que a thread anterior
    morreu — abrir mesmo assim duplicaria a conexão RTSP com a câmera física."""
    if not parar_camera(camera_db_id):
        log.error(
            "Câmera %d: reinício ABORTADO — thread do pipeline anterior ainda viva; "
            "NÃO abrindo uma segunda conexão RTSP concorrente. Tentativa seguinte "
            "(supervisor/backoff) pode ter sucesso quando a thread antiga morrer.",
            camera_db_id,
        )
        return False
    iniciar_camera(camera_db_id, cfg)
    return True


def parar_todas() -> None:
    for p in list(_instancias.values()):
        p.parar()
    _instancias.clear()
    with estado.lock:
        estado.frames_cameras.clear()
        estado.frames_cameras_limpos.clear()
    from app.streaming import stream as stream_mod
    stream_mod.limpar_cache()


def parar() -> None:
    parar_todas()


def reiniciar(cfg: dict[str, str]) -> None:
    """Para todas as instâncias e reinicia com a nova config."""
    parar_todas()
    iniciar_cameras_db(cfg)
