"""Detector de placas via ONNX Runtime.

Suporta dois formatos de saída automaticamente:
  - YOLO26 end-to-end (padrão): (N, 300, 6) → xyxy + conf + cls, sem NMS
  - YOLOv8/v9/v10/v11:         (N, nc+4, 8400) → cxcywh + classes, requer NMS

Quando o modelo não está disponível, faz fallback para detecção por contornos
(menos preciso, útil em desenvolvimento sem o .onnx baixado).
"""
from __future__ import annotations
import logging
import math
import threading
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from app.visao import _fabrica_singleton as _fab
from app.visao.contexto_log import ContadorDeFalhas

log = logging.getLogger(__name__)

INPUT_SIZE = 640


@dataclass(frozen=True, slots=True)
class OrigemTipo:
    """De onde veio (ou por que NÃO veio) o tipo do veículo desta placa.

    `deteccoes.tipo_veiculo` é só o veredito ('moto'/'carro'/None); este objeto carrega
    junto o sinal CRU que o produziu — mesmo precedente de `acordo` (medida) +
    `confirmada` (veredito congelado) em `app/core/banco/_esquema.py`. Sem o sinal cru,
    "subir `veiculo_conf` custaria o quê?" e "quanto do NULL é falta de veículo vs. 2
    estágios desligado?" só têm resposta por palpite.

    `fonte` é o vocabulário gravado em `deteccoes.tipo_veiculo_fonte` — cresce (o replay
    de `testes/recalcula_tipo_veiculo.py` usa o prefixo `replay:`), por isso é validado em
    Python (`banco/_deteccoes.py`), não por CHECK de coluna: um CHECK não dá para crescer
    sem recriar a tabela.

        'veiculo'             classificado a partir de veículo detectado (tipo preenchido)
        'classe-nao-mapeada'  veículo detectado, classe fora de TIPO_POR_CLASSE (tipo None)
        'sem-veiculo'         2 estágios rodou, nenhum veículo no quadro (tipo None)
        'tiles'               placa veio da varredura em janelas — não roda o estágio de
                              veículo (tipo None)
        'sem-2-estagios'      detector de 1 estágio (tipo None) — é o default quando a bbox
                              não carrega o atributo (ver `origem_de_bbox`)
        'veiculo-ambiguo'     a placa caiu dentro de dois veículos de classes DIFERENTES
                              (tipo None) — ver `DetectorDoisEstagios._dedup`
        'track-sem-deteccao'  track emitido sem detecção nova neste quadro (tipo None) —
                              ver `pipeline._origem_do_track`
    """
    fonte: str
    tipo: str | None = None
    classe: int | None = None
    conf: float | None = None

    @classmethod
    def de_classe(cls, classe: int, conf: float) -> "OrigemTipo":
        """A partir da classe COCO crua de um veículo detectado."""
        tipo = VehicleDetector.TIPO_POR_CLASSE.get(classe)
        fonte = "veiculo" if tipo is not None else "classe-nao-mapeada"
        return cls(fonte=fonte, tipo=tipo, classe=classe, conf=conf)


# Singletons dos casos sem veículo — frozen, então seguros para reusar em toda detecção.
SEM_VEICULO = OrigemTipo(fonte="sem-veiculo")
TILES = OrigemTipo(fonte="tiles")
SEM_2_ESTAGIOS = OrigemTipo(fonte="sem-2-estagios")
# A mesma placa caiu dentro de dois veículos de classes diferentes (ver
# `DetectorDoisEstagios._dedup`): a associação estrutural deixou de ser única, então não há
# tipo a afirmar. Guardado como causa própria para a consulta de NULL saber diferenciar
# "não vi veículo" de "vi dois e eles discordam".
VEICULO_AMBIGUO = OrigemTipo(fonte="veiculo-ambiguo")
# O track foi emitido sem detecção nova neste quadro que casasse com ele (ByteTrack
# prevendo por Kalman durante oclusão). Não há veículo OBSERVADO agora para afirmar o tipo,
# e isso é diferente de "não vi veículo" — ver `pipeline._origem_do_track`.
TRACK_SEM_DETECCAO = OrigemTipo(fonte="track-sem-deteccao")


class BBoxPlaca(tuple):
    """Bbox de placa `(x, y, w, h, conf)` que carrega, à parte, a origem do tipo do veículo.

    Subclasse de `tuple` de tamanho 5 DE PROPÓSITO, e não uma 6-tupla: todo consumidor
    desempacota cinco posicionalmente (`pipeline._processar_classico`, `leitura._ler_placa`,
    `Tracker.update`, o harness de acurácia, os dublês de teste) e alguns comparam o
    resultado com tupla crua por igualdade. Assim a origem viaja POR PLACA sem que nada
    disso mude.

    Por que por placa, e não num atributo do detector: um quadro de posto tem 2 a 4
    veículos, então `self._ultima_origem` responderia sempre pelo último do laço. E o
    detector de leitura é um singleton cacheado cujo lock é solto antes de quem chamou ler
    o atributo — com duas câmeras, uma leria o valor da outra.

    `origem` é `None` em toda bbox que NÃO saiu de dentro de um veículo detectado
    (fallback do 2 estágios, varredura em janelas, detector de 1 estágio) — use
    `origem_de_bbox()`/`tipo_de_bbox()`, que tratam essa ausência como `SEM_2_ESTAGIOS`.
    Nunca 'carro' por omissão.

    Reconstruir a tupla DESCARTA o atributo (`tuple(bb)`, `(bb[0] + dx, ...)`); use
    `deslocar()`. O modo de falha é degradar para "não estimado", nunca inventar tipo.

    `__slots__` não é possível aqui: `nonempty __slots__ not supported for subtype of
    'tuple'`. O `__dict__` custa 16 bytes por bbox.
    """

    def __new__(cls, x, y, w, h, conf, origem: OrigemTipo | None = None):
        obj = super().__new__(cls, (int(x), int(y), int(w), int(h), float(conf)))
        obj.origem = origem
        return obj


def origem_de_bbox(bb) -> OrigemTipo:
    """Origem do tipo de veículo desta bbox — nunca None.

    `getattr` e não atributo direto porque tupla crua é entrada legítima: detector de 1
    estágio, busca em janelas e dublês de teste devolvem tuplas comuns, e a ausência do
    atributo (ou o valor `None` explícito) significa honestamente `SEM_2_ESTAGIOS`.
    """
    return getattr(bb, "origem", None) or SEM_2_ESTAGIOS


def tipo_de_bbox(bb) -> str | None:
    """Tipo do veículo de uma bbox, ou None. Atalho sobre `origem_de_bbox(bb).tipo`."""
    return origem_de_bbox(bb).tipo


def deslocar(bb, dx: int, dy: int) -> BBoxPlaca:
    """Bbox transladada PRESERVANDO a origem do tipo.

    O recorte por ROI acontece em dois caminhos (`Pipeline._processar_frame` e
    `leitura._detectar`) e a comprehension crua que existia nos dois — `[(x + rx, y + ry,
    w, h, c) for ...]` — descartava o atributo em silêncio.
    """
    return BBoxPlaca(bb[0] + dx, bb[1] + dy, bb[2], bb[3], bb[4], getattr(bb, "origem", None))


class Detector:
    def __init__(self, modelo_path: str, conf: float = 0.5, nms: float = 0.4):
        self.modelo_path = Path(modelo_path)
        self.conf = conf
        self.nms = nms
        self.sess = None
        self.input_name: str | None = None

    def carregar(self) -> None:
        if self.sess is not None:
            return          # idempotente — ver a nota em `BuscaEmTiles.carregar`
        if not self.modelo_path.exists():
            log.warning("Modelo %s não encontrado — usando fallback por contornos", self.modelo_path)
            return
        try:
            import onnxruntime as ort
            from app.visao.hardware import onnx_providers
            self.sess = ort.InferenceSession(str(self.modelo_path), providers=onnx_providers())
            self.input_name = self.sess.get_inputs()[0].name
            out_shape = self.sess.get_outputs()[0].shape
            fmt = "YOLO26 e2e" if (len(out_shape) == 3 and out_shape[-1] == 6) else "YOLOv8"
            log.info("ONNX carregado [%s]: %s", fmt, self.modelo_path)
        except Exception as e:
            log.error("Falha ao carregar ONNX (%s) — usando fallback", e)
            self.sess = None

    def detectar(self, frame) -> list[tuple[int, int, int, int, float]]:
        """Retorna lista de bboxes [(x, y, w, h, conf)]."""
        if self.sess is None:
            return self._fallback_contornos(frame)
        return self._inferir_yolo(frame)

    def _inferir_yolo(self, frame) -> list[tuple[int, int, int, int, float]]:
        h0, w0 = frame.shape[:2]
        img = cv2.resize(frame, (INPUT_SIZE, INPUT_SIZE))
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        img = img.transpose(2, 0, 1)[None, ...]

        out = self.sess.run(None, {self.input_name: img})[0]

        # YOLO26 end-to-end: (1, 300, 6) → x1,y1,x2,y2,conf,cls (xyxy, sem NMS)
        if out.ndim == 3 and out.shape[-1] == 6:
            return self._parsear_yolo26_e2e(out, h0, w0)

        # YOLOv8/v9/v10/v11: (1, nc+4, 8400) → cxcywh + classes, requer NMS
        return self._parsear_yolov8(out, h0, w0)

    def _parsear_yolo26_e2e(self, out, h0, w0) -> list[tuple[int, int, int, int, float]]:
        """YOLO26 one-to-one: (1,300,6) x1,y1,x2,y2,conf,cls em coordenadas INPUT_SIZE."""
        preds = np.squeeze(out)  # (300, 6)
        mask = preds[:, 4] > self.conf
        preds = preds[mask]
        boxes = []
        for x1, y1, x2, y2, s, _ in preds:
            x = int(x1 * w0 / INPUT_SIZE)
            y = int(y1 * h0 / INPUT_SIZE)
            w_ = int((x2 - x1) * w0 / INPUT_SIZE)
            h_ = int((y2 - y1) * h0 / INPUT_SIZE)
            boxes.append((x, y, w_, h_, float(s)))
        return boxes

    def _parsear_yolov8(self, out, h0, w0) -> list[tuple[int, int, int, int, float]]:
        """YOLOv8/v9/v10/v11: (1,nc+4,8400) cxcywh + classes, aplica NMS."""
        preds = np.squeeze(out).T  # (8400, nc+4)
        scores = preds[:, 4:].max(axis=1) if preds.shape[1] > 4 else preds[:, 4]
        mask = scores > self.conf
        preds = preds[mask]
        scores = scores[mask]
        if len(preds) == 0:
            return []

        boxes = []
        for (cx, cy, w, h), s in zip(preds[:, :4], scores):
            x = int((cx - w / 2) * w0 / INPUT_SIZE)
            y = int((cy - h / 2) * h0 / INPUT_SIZE)
            w_ = int(w * w0 / INPUT_SIZE)
            h_ = int(h * h0 / INPUT_SIZE)
            boxes.append([x, y, w_, h_, float(s)])

        idx = cv2.dnn.NMSBoxes(
            [b[:4] for b in boxes],
            [b[4] for b in boxes],
            self.conf,
            self.nms,
        )
        if len(idx) == 0:
            return []
        idx = idx.flatten() if hasattr(idx, "flatten") else idx
        return [tuple(boxes[i]) for i in idx]

    def _fallback_contornos(self, frame) -> list[tuple[int, int, int, int, float]]:
        cinza = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        bordas = cv2.Canny(cinza, 50, 200)
        contornos, _ = cv2.findContours(bordas, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        candidatos: list[tuple[int, int, int, int, float]] = []
        for c in contornos:
            x, y, w, h = cv2.boundingRect(c)
            if w < 80 or h < 25:
                continue
            aspect = w / max(h, 1)
            if 2.0 <= aspect <= 5.0 and w * h < frame.shape[0] * frame.shape[1] * 0.3:
                candidatos.append((x, y, w, h, 0.4))
        candidatos.sort(key=lambda b: b[2] * b[3], reverse=True)
        return candidatos[:3]


class MultiDetector:
    """Executa múltiplos modelos YOLO e une detecções por sobreposição (IoU).

    Bboxes detectadas por vários modelos na mesma região são fundidas numa só,
    com confiança boosted. Quando votos_minimos > 1, descarta detecções que
    apenas um modelo viu (reduz falsos positivos).

    Interface compatível com Detector (mesmo .detectar() e .carregar()).
    """

    IOU_LIMIAR = 0.30  # IoU mínimo para considerar que dois modelos detectaram a mesma placa

    def __init__(self, detectors: list[Detector], votos_minimos: int = 1):
        self._detectors = detectors
        self.votos_minimos = max(1, votos_minimos)
        self.sess = detectors[0].sess if detectors else None  # compatibilidade com estado.modelo_carregado

    def carregar(self) -> None:
        for d in self._detectors:
            d.carregar()
        self.sess = self._detectors[0].sess if self._detectors else None

    def detectar(self, frame) -> list[tuple[int, int, int, int, float]]:
        # Coleta todas as bboxes com índice do modelo que as gerou
        todas: list[tuple[tuple, int]] = []
        for idx, det in enumerate(self._detectors):
            for bbox in det.detectar(frame):
                todas.append((bbox, idx))

        if not todas:
            return []

        n_modelos = len(self._detectors)
        usados = [False] * len(todas)
        resultado: list[tuple[int, int, int, int, float]] = []

        for i, (bbox_i, mod_i) in enumerate(todas):
            if usados[i]:
                continue
            grupo_bboxes = [bbox_i]
            grupo_mods = {mod_i}
            usados[i] = True

            for j, (bbox_j, mod_j) in enumerate(todas):
                if usados[j]:
                    continue
                if self._iou(bbox_i, bbox_j) >= self.IOU_LIMIAR:
                    grupo_bboxes.append(bbox_j)
                    grupo_mods.add(mod_j)
                    usados[j] = True

            n_votos = len(grupo_mods)
            if n_votos < self.votos_minimos:
                log.debug("Bbox descartada: apenas %d/%d modelos detectaram", n_votos, n_modelos)
                continue

            # Funde: posição média, confiança máxima + boost pelo grau de concordância
            n = len(grupo_bboxes)
            x = int(sum(b[0] for b in grupo_bboxes) / n)
            y = int(sum(b[1] for b in grupo_bboxes) / n)
            w = int(sum(b[2] for b in grupo_bboxes) / n)
            h = int(sum(b[3] for b in grupo_bboxes) / n)
            conf_base = max(b[4] for b in grupo_bboxes)
            boost = 1.0 + 0.15 * (n_votos - 1) / max(n_modelos - 1, 1)
            resultado.append((x, y, w, h, round(min(1.0, conf_base * boost), 3)))

        return resultado

    @staticmethod
    def _iou(a: tuple, b: tuple) -> float:
        ax, ay, aw, ah = a[0], a[1], a[2], a[3]
        bx, by, bw, bh = b[0], b[1], b[2], b[3]
        ix1, iy1 = max(ax, bx), max(ay, by)
        ix2, iy2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
        if ix2 <= ix1 or iy2 <= iy1:
            return 0.0
        inter = (ix2 - ix1) * (iy2 - iy1)
        return inter / (aw * ah + bw * bh - inter)


class OpenImageDetector:
    """Detector de placas via `open-image-models` (licença MIT, ONNX YOLOv9-t).

    Backend comercialmente-permissivo — alternativa aos modelos Ultralytics (AGPL).
    A própria biblioteca cuida do pré/pós-processamento (letterbox + NMS end2end),
    garantindo coordenadas corretas. Interface compatível com `Detector`
    (mesmo `.carregar()`, `.detectar()` e atributo `.sess`).

    Modelos disponíveis (input size ↓ = mais rápido, ↑ = mais preciso):
      yolo-v9-t-256/384/416/512/640-license-plate-end2end · yolo-v9-s-608-...
    """

    def __init__(self, modelo: str = "yolo-v9-t-384-license-plate-end2end", conf: float = 0.25):
        self.modelo = modelo
        self.conf = conf
        self.sess = None      # InferenceSession subjacente (p/ estado.modelo_carregado)
        self._det = None
        self._falhas = ContadorDeFalhas("open-image-models")

    def carregar(self) -> None:
        if self._det is not None:
            return          # idempotente — ver a nota em `BuscaEmTiles.carregar`
        try:
            from open_image_models import LicensePlateDetector
        except ImportError:
            log.error("open-image-models não instalado — rode: pip install open-image-models")
            self.sess = None
            return
        try:
            from app.visao.hardware import onnx_providers
            self._det = LicensePlateDetector(detection_model=self.modelo, conf_thresh=self.conf,
                                              providers=onnx_providers())
            self.sess = getattr(self._det, "model", None)
            log.info("open-image-models carregado [MIT]: %s (conf≥%.2f)", self.modelo, self.conf)
        except Exception as e:
            log.error("Falha ao carregar open-image-models (%s) — detecção desativada", e)
            self.sess = None
            self._det = None

    def detectar(self, frame) -> list[tuple[int, int, int, int, float]]:
        if self._det is None:
            return []
        try:
            results = self._det.predict(frame)
        except Exception as e:
            # Uma falha isolada e DEBUG; dez seguidas viram um ERROR unico. Ver
            # `ContadorDeFalhas` - 849 WARNINGs iguais num processo nao informaram nada.
            self._falhas.falhou(e)
            return []
        self._falhas.funcionou()
        boxes: list[tuple[int, int, int, int, float]] = []
        for r in results:
            bb = r.bounding_box
            x, y = int(bb.x1), int(bb.y1)
            boxes.append((x, y, int(bb.x2 - bb.x1), int(bb.y2 - bb.y1), float(r.confidence)))
        return boxes


class VehicleDetector:
    """Detector de veículos via YOLOX-s ONNX (OpenCV Model Zoo, licença Apache-2.0).

    Primeiro estágio da detecção em 2 estágios (veículo → placa): restringe a busca de
    placa à região de um veículo detectado, eliminando falsos positivos fora de veículos
    (texto de fundo, placas de exemplo em telas) e melhorando placa pequena/distante
    (busca num recorte de maior resolução relativa).

    Saída (1,8400,85) = 4 coords (grid-relativas) + objectness + 80 classes COCO.
    Decode replica o `yolox.py` de referência do OpenCV Zoo: letterbox (pad 114, sem
    normalização), grid+stride, NMS por classe. Filtra às classes de veículo.
    """

    INPUT_SIZE = 640
    STRIDES = (8, 16, 32)
    CLASSES_COCO = {
        2: "car", 3: "motorcycle", 5: "bus", 7: "truck",
    }

    # Classe COCO → `deteccoes.tipo_veiculo`. A pergunta num posto é duas rodas vs quatro
    # (o bico, o abastecimento, o layout da placa), então ônibus e caminhão entram como
    # 'carro' em vez de virarem categoria própria: o YOLOX chama picape de `truck`, e
    # criar 'caminhao' com 4,2% de amostra medida seria calibrar sem medida. Se um dia
    # houver demanda, é uma linha aqui MAIS quatro pontos que precisam mudar juntos:
    # validação em `banco/_deteccoes.py` (registrar/atualizar), o filtro SQL do mesmo
    # arquivo, o `Literal` de `web/api.py` e o <select>+ternário de `historico.html`.
    #
    # `.get()` e não indexação: `veiculo_classes` é configurável, e uma classe fora do
    # mapa (ex.: 1=bicycle) tem que virar None — não estourar dentro do laço de detecção.
    TIPO_POR_CLASSE = {2: "carro", 3: "moto", 5: "carro", 7: "carro"}

    def __init__(self, modelo_path: str, conf: float = 0.4, nms: float = 0.5,
                 classes: frozenset[int] | None = None):
        self.modelo_path = Path(modelo_path)
        self.conf = conf
        self.nms = nms
        self.classes = classes if classes is not None else frozenset(self.CLASSES_COCO)
        self.sess = None
        self.input_name: str | None = None
        self._grids, self._strides_exp = self._gerar_grids()
        self._falhas = ContadorDeFalhas("VehicleDetector")

    @classmethod
    def _gerar_grids(cls):
        grids, strides_exp = [], []
        for stride in cls.STRIDES:
            hsize = wsize = cls.INPUT_SIZE // stride
            xv, yv = np.meshgrid(np.arange(hsize), np.arange(wsize))
            grid = np.stack((xv, yv), 2).reshape(1, -1, 2)
            grids.append(grid)
            strides_exp.append(np.full((*grid.shape[:2], 1), stride))
        return np.concatenate(grids, 1)[0], np.concatenate(strides_exp, 1)[0]

    def carregar(self) -> None:
        if self.sess is not None:
            return          # idempotente — ver a nota em `BuscaEmTiles.carregar`
        if not self.modelo_path.exists():
            log.warning("VehicleDetector: modelo %s não encontrado — 2 estágios cairá "
                        "sempre no fallback (frame inteiro)", self.modelo_path)
            return
        try:
            import onnxruntime as ort
            from app.visao.hardware import onnx_providers
            self.sess = ort.InferenceSession(str(self.modelo_path), providers=onnx_providers())
            self.input_name = self.sess.get_inputs()[0].name
            log.info("VehicleDetector carregado [YOLOX-s, Apache-2.0]: %s", self.modelo_path)
            # Aquece o grafo ONNX no carregamento (não na 1ª detecção real) — ORT otimiza
            # o grafo na primeira execução, o que custaria ~200ms extras à primeira leitura.
            try:
                self.detectar(np.zeros((self.INPUT_SIZE, self.INPUT_SIZE, 3), dtype=np.uint8))
            except Exception:
                pass
        except Exception as e:
            log.error("Falha ao carregar VehicleDetector (%s) — 2 estágios desativado", e)
            self.sess = None

    def detectar(self, frame) -> list[tuple[int, int, int, int, float, int]]:
        """Retorna bboxes de veículo (x,y,w,h,conf,classe_coco) em coords do frame original.

        A classe vem junto porque é o único lugar do sistema que SABE se o veículo é moto
        ou carro — ela já era calculada aqui (o argmax do modelo) e descartada na saída,
        e o tipo acabava sendo readivinhado depois pelo aspecto do recorte da placa, que
        mede a folga do detector e não a diagramação da placa.
        """
        if self.sess is None or frame is None or frame.size == 0:
            return []
        try:
            veiculos = self._inferir(frame)
        except Exception as e:
            self._falhas.falhou(e)
            return []
        self._falhas.funcionou()
        return veiculos

    def _inferir(self, frame) -> list[tuple[int, int, int, int, float, int]]:
        h0, w0 = frame.shape[:2]
        S = self.INPUT_SIZE
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        ratio = min(S / h0, S / w0)
        nova_w, nova_h = int(w0 * ratio), int(h0 * ratio)
        resized = cv2.resize(rgb, (nova_w, nova_h), interpolation=cv2.INTER_LINEAR).astype(np.float32)
        padded = np.full((S, S, 3), 114.0, dtype=np.float32)
        padded[:nova_h, :nova_w] = resized
        blob = padded.transpose(2, 0, 1)[None, ...]

        out = self.sess.run(None, {self.input_name: blob})[0]
        dets = np.squeeze(out, axis=0).copy()
        dets[:, :2] = (dets[:, :2] + self._grids) * self._strides_exp
        dets[:, 2:4] = np.exp(dets[:, 2:4]) * self._strides_exp

        boxes = np.empty_like(dets[:, :4])
        boxes[:, 0] = dets[:, 0] - dets[:, 2] / 2
        boxes[:, 1] = dets[:, 1] - dets[:, 3] / 2
        boxes[:, 2] = dets[:, 0] + dets[:, 2] / 2
        boxes[:, 3] = dets[:, 1] + dets[:, 3] / 2

        scores = dets[:, 4:5] * dets[:, 5:]
        max_scores = scores.max(axis=1)
        max_idx = scores.argmax(axis=1)

        mask = (max_scores > self.conf) & np.isin(max_idx, list(self.classes))
        if not mask.any():
            return []
        b, s, c = boxes[mask], max_scores[mask], max_idx[mask]

        keep = cv2.dnn.NMSBoxesBatched(b.tolist(), s.tolist(), c.tolist(), self.conf, self.nms)
        keep = keep.flatten() if hasattr(keep, "flatten") else keep
        if len(keep) == 0:
            return []

        resultado = []
        for i in keep:
            x0, y0, x1, y1 = b[i] / ratio   # unletterbox: sem offset, padding é sempre embaixo/direita
            x0, y0 = max(0, int(x0)), max(0, int(y0))
            x1, y1 = min(w0, int(x1)), min(h0, int(y1))
            if x1 > x0 and y1 > y0:
                resultado.append((x0, y0, x1 - x0, y1 - y0, float(s[i]), int(c[i])))
        return resultado


class DetectorDoisEstagios:
    """Detecção em 2 estágios: detecta o VEÍCULO primeiro, busca a placa só dentro dele.

    Padrão usado por ALPR comerciais (OpenALPR, Genetec) — elimina falsos positivos de
    região "tipo placa" fora de qualquer veículo (texto de fundo, placas de exemplo em
    telas/páginas) e melhora placa pequena/distante (busca num recorte de maior resolução
    relativa ao invés do frame inteiro).

    Fallback SEGURO (padrão): se nenhum veículo for detectado, cai para a busca de placa
    no frame inteiro — nunca fica pior que a detecção de 1 estágio. `obrigatorio=True`
    torna o filtro estrito (sem veículo → sem placa).

    Interface idêntica a `Detector` (.carregar(), .detectar(), .sess).
    """

    def __init__(self, detector_placa, detector_veiculo: VehicleDetector,
                 padding: float = 0.05, obrigatorio: bool = False, max_veiculos: int = 5):
        self.detector_placa = detector_placa
        self.detector_veiculo = detector_veiculo
        self.padding = padding
        self.obrigatorio = obrigatorio
        # Limita quantos veículos disparam uma busca de placa por frame — cenas movimentadas
        # (estacionamento/rua) podem ter várias dezenas de veículos e cada um roda o detector
        # de placa; sem limite, a latência cresce linearmente com a contagem de veículos.
        # Prioriza os MAIORES (mais próximos da câmera = mais prováveis de ser o alvo).
        self.max_veiculos = max(1, max_veiculos)
        self.sess = None

    def carregar(self) -> None:
        self.detector_placa.carregar()
        self.detector_veiculo.carregar()
        self.sess = self.detector_placa.sess   # compat: estado.modelo_carregado reflete a placa

    def detectar(self, frame) -> list[tuple[int, int, int, int, float]]:
        if frame is None or frame.size == 0:
            return []
        veiculos = self.detector_veiculo.detectar(frame)
        if not veiculos:
            if self.obrigatorio:
                return []
            # Fallback: não houve veículo, logo não há classe para afirmar — `SEM_VEICULO`
            # é a resposta honesta, e não a ausência silenciosa de atributo (essa
            # significaria "nem passou pelo 2 estágios", que é uma causa DIFERENTE de
            # NULL — ver `SEM_2_ESTAGIOS`).
            return [BBoxPlaca(*b, SEM_VEICULO)
                    for b in self.detector_placa.detectar(frame)]   # fallback: frame inteiro

        if len(veiculos) > self.max_veiculos:
            veiculos = sorted(veiculos, key=lambda v: v[2] * v[3], reverse=True)[: self.max_veiculos]

        f_h, f_w = frame.shape[:2]
        placas: list[tuple[int, int, int, int, float]] = []
        for vx, vy, vw, vh, vconf, vcls in veiculos:
            # A associação placa→tipo é ESTRUTURAL: esta placa foi encontrada dentro do
            # recorte DESTE veículo. Não é uma contenção geométrica redescoberta depois,
            # que erraria com veículos sobrepostos.
            origem = OrigemTipo.de_classe(vcls, vconf)
            log.debug("Veículo %s (classe %d, conf=%.2f) → tipo_veiculo=%s",
                      VehicleDetector.CLASSES_COCO.get(vcls, "?"), vcls, vconf, origem.tipo)
            dx, dy = int(vw * self.padding), int(vh * self.padding)
            x0, y0 = max(0, vx - dx), max(0, vy - dy)
            x1, y1 = min(f_w, vx + vw + dx), min(f_h, vy + vh + dy)
            crop = frame[y0:y1, x0:x1]
            if crop.size == 0:
                continue
            for px, py, pw, ph, pconf in self.detector_placa.detectar(crop):
                placas.append(BBoxPlaca(px + x0, py + y0, pw, ph, pconf, origem))

        if len(veiculos) > 1 and len(placas) > 1:
            placas = self._dedup(placas)
        return placas

    @staticmethod
    def _dedup(placas: list[tuple[int, int, int, int, float]]) -> list[tuple[int, int, int, int, float]]:
        """Remove placas duplicadas de veículos sobrepostos (mantém a de maior confiança).

        INVARIANTE: não RECONSTRÓI a tupla para preservar coordenadas/confiança — é o que
        mantém o `origem` das `BBoxPlaca`. Uma comprehension que remonte
        `(x, y, w, h, conf)` derrubaria o tipo em silêncio, e o sintoma apareceria longe
        daqui: histórico inteiro em "Não estimado".

        A ÚNICA reescrita permitida é a de desempate de tipo, abaixo. Quando a MESMA placa
        é achada dentro de dois veículos sobrepostos de CLASSES diferentes (moto no bico 5
        com um carro atrás, caixas se cruzando no plano da imagem — o recorte do carro
        contém a placa da moto), sobreviver "a de maior confiança de placa" escolhia o tipo
        pela confiança do detector de PLACA, que nada diz sobre de quem é o veículo. Dava
        para gravar a placa da moto como 'carro' com `fonte='veiculo'`: um erro afirmativo,
        que é pior que NULL. Aqui isso vira ambíguo — a associação estrutural deixou de ser
        única, e o honesto é dizer que não se sabe.
        """
        placas = sorted(placas, key=lambda p: -p[4])
        mantidas: list[tuple[int, int, int, int, float]] = []
        for p in placas:
            duplicada_de = next(
                (m for m in mantidas if MultiDetector._iou(p, m) >= 0.5), None)
            if duplicada_de is None:
                mantidas.append(p)
                continue
            # Mesma placa, dois veículos: se discordam do tipo, ninguém ganha.
            tipo_mantido = tipo_de_bbox(duplicada_de)
            tipo_descartado = tipo_de_bbox(p)
            if tipo_mantido != tipo_descartado and None not in (tipo_mantido, tipo_descartado):
                log.debug("Placa em %d veículos de classes diferentes (%s vs %s) — tipo ambíguo",
                          2, tipo_mantido, tipo_descartado)
                # Índice por IDENTIDADE, não por conteúdo. `BBoxPlaca` é subclasse de
                # `tuple`, e `list.index` compara elemento a elemento — ignorando o atributo
                # `origem`. Duas caixas com os mesmos (x, y, w, h, conf) e origens diferentes
                # colidem, e a marca de "tipo ambíguo" cai na errada.
                # `consenso.agrupar_por_veiculo` documenta essa exata armadilha e escolheu
                # `is` pelo mesmo motivo. (Auditoria 27/08/2026.)
                _i = next(i for i, m in enumerate(mantidas) if m is duplicada_de)
                mantidas[_i] = BBoxPlaca(*duplicada_de[:5], VEICULO_AMBIGUO)
        return mantidas


class BuscaEmTiles:
    """Reexamina o frame em JANELAS SOBREPOSTAS quando a passada única não achou nada.

    O detector de placa faz letterbox de tudo o que recebe para o lado do input do seu
    modelo (608 no s-608). Numa ROI grande, a placa de uma moto parada na bomba (~38px
    num frame 1280x720) chega ao modelo pequena demais e não é detectada.

    Medido em cena real (moto no bico 5, placa de 38x35px numa ROI de 397x610):

      * passada única na ROI ......................... nada, mesmo com conf 0.05
      * ROI ampliada 2x/3x antes de detectar ......... nada (inútil por construção:
        o modelo redimensiona de volta para 608, a placa volta ao mesmo tamanho)
      * recorte fechado só na placa (38..153px) ...... nada — não é só escala, o modelo
        precisa do CONTEXTO do veículo em volta
      * janelas de ~250x300px pegando moto+placa ..... conf 0.4 a 0.8

    Ou seja: o que recupera essa placa é VARIAR O ENQUADRAMENTO, não a resolução. E é
    exatamente o que o loop de leitura (leitura.py) nunca fazia: ele repete no TEMPO,
    mas com a ROI do bico fixa o enquadramento é sempre o mesmo — para uma moto parada,
    as 12 tentativas eram 12 recortes idênticos, logo 12 falhas idênticas.

    `sobreposicao` tem uma faixa útil ESTREITA, e o motivo é geométrico, não empírico:
    quanto maior a sobreposição, maior cada janela — em 0.5 a janela já é quase a ROI
    inteira, que é exatamente o enquadramento que falhou. Na mesma cena, 0.25-0.35
    acham a placa e 0.20/0.40/0.50 não acham nada. Mexer nisso sem medir provavelmente
    piora; o valor padrão está no centro da faixa que funciona.

    Custo: zero na maioria das leituras (só entra quando a passada normal deu nada) e
    ~`max_janelas` passadas extras do detector de placa quando entra (~200ms cada em
    CPU). Por isso é ligado no GET, que tolera a latência, e não no stream ao vivo.

    Interface idêntica a `Detector` (.carregar(), .detectar(), .sess).
    """

    def __init__(self, detector, detector_tiles=None, lado_alvo: int = 300,
                 sobreposicao: float = 0.30, max_janelas: int = 6):
        self.detector = detector
        # Nas janelas roda só o estágio de PLACA. Repetir o estágio de veículo por janela
        # multiplicaria a latência sem ganho: se ele fosse achar o veículo, a passada
        # normal já teria achado — é justamente o veículo não detectado (moto ocluída,
        # que o YOLOX classifica como `bicycle` ou nem vê) que traz o fluxo até aqui.
        # Costuma vir com limiar de confiança MAIS BAIXO que o detector principal (ver
        # `tiles_conf`): aqui já se sabe que o caminho normal não achou nada, e a placa
        # nessas janelas sai raspando o limiar (0.19-0.37 na cena medida). Baixar o limiar
        # amplia a faixa de enquadramentos que registram algo, e o custo de um recorte
        # ruim é baixo — ele ainda tem que passar pelo OCR, por `validar()` e pelo
        # consenso entre frames antes de virar uma leitura. Medido na cena real: nenhum
        # falso positivo até 0.10 (só a placa certa aparecia).
        self.detector_tiles = detector_tiles if detector_tiles is not None else detector
        self.lado_alvo = max(64, lado_alvo)
        self.sobreposicao = min(max(sobreposicao, 0.0), 0.9)
        self.max_janelas = max(1, max_janelas)
        self.sess = None

    def carregar(self) -> None:
        """Carrega os dois detectores.

        Chama `carregar()` nos dois SEM checar se são o mesmo objeto: a guarda anterior
        (`detector_tiles is not detector`) comparava identidade e não conseguia ver que
        `detector_tiles` costuma estar ANINHADO dentro de `detector` — em produção
        `detector` é o `DetectorDoisEstagios` e `detector_tiles` é o detector de placa que
        vive dentro dele. A identidade passava, o modelo carregava duas vezes, e a primeira
        sessão ONNX ficava presa para sempre por trás de `.sess`.

        A idempotência vive agora em cada `carregar()` (todos saem cedo se já carregaram),
        que resolve o caso aninhado em qualquer profundidade — e não só o de um nível.
        """
        self.detector.carregar()
        self.detector_tiles.carregar()
        self.sess = self.detector.sess

    def detectar(self, frame) -> list[tuple[int, int, int, int, float]]:
        if frame is None or frame.size == 0:
            return []
        achados = self.detector.detectar(frame)
        if achados:
            # Caminho normal: repassa intacto o que o 2 estágios devolveu, `BBoxPlaca`
            # com tipo inclusive. Não remontar as tuplas aqui.
            return achados

        # Daqui para baixo o tipo do veículo é sempre None, de propósito: as janelas rodam
        # SÓ o estágio de placa (ver o docstring da classe — o estágio de veículo não é
        # varrido por janela porque, se ele fosse achar o veículo, a passada normal já
        # teria achado). `TILES`, e não `SEM_VEICULO`: são causas diferentes de NULL — aqui
        # o estágio de veículo nem chegou a rodar.
        janelas = self._janelas(frame.shape[1], frame.shape[0])
        if not janelas:
            return []
        for x0, y0, x1, y1 in janelas:
            tile = frame[y0:y1, x0:x1]
            if tile.size == 0:
                continue
            for px, py, pw, ph, pconf in self.detector_tiles.detectar(tile):
                achados.append(BBoxPlaca(px + x0, py + y0, pw, ph, pconf, TILES))

        if achados:
            log.info("BuscaEmTiles: %d placa(s) recuperada(s) em %d janela(s) — a passada "
                     "única no recorte de %dx%d não tinha achado nada",
                     len(achados), len(janelas), frame.shape[1], frame.shape[0])
        # Uma placa perto da divisa entre janelas aparece nas duas (é para isso que existe
        # a sobreposição); _dedup mantém a de maior confiança.
        return DetectorDoisEstagios._dedup(achados) if len(achados) > 1 else achados

    def _janelas(self, w: int, h: int) -> list[tuple[int, int, int, int]]:
        """Grade de janelas sobrepostas cobrindo o frame, respeitando `max_janelas`."""
        nx = max(1, math.ceil(w / self.lado_alvo))
        ny = max(1, math.ceil(h / self.lado_alvo))
        # Cabe em `max_janelas`? Se não, engrossa as janelas (tira divisões do eixo mais
        # dividido) em vez de deixar parte do frame sem varrer.
        while nx * ny > self.max_janelas and (nx > 1 or ny > 1):
            if nx >= ny and nx > 1:
                nx -= 1
            else:
                ny -= 1
        if nx * ny <= 1:
            return []   # 1 janela = a mesma passada que já falhou; não repete de graça

        pw, ph = w / nx, h / ny
        ox, oy = pw * self.sobreposicao, ph * self.sobreposicao
        janelas = []
        for iy in range(ny):
            for ix in range(nx):
                x0 = max(0, int(ix * pw - ox))
                y0 = max(0, int(iy * ph - oy))
                x1 = min(w, int((ix + 1) * pw + ox))
                y1 = min(h, int((iy + 1) * ph + oy))
                janelas.append((x0, y0, x1, y1))
        return janelas


def _criar_detector_veiculo(cfg: dict) -> VehicleDetector:
    classes = frozenset(
        int(c) for c in cfg.get("veiculo_classes", "2,3,5,7").split(",") if c.strip().isdigit()
    )
    return VehicleDetector(
        modelo_path=cfg.get("veiculo_modelo_path", "models/vehicle_detector.onnx"),
        conf=float(cfg.get("veiculo_conf", "0.4")),
        nms=float(cfg.get("veiculo_nms", "0.5")),
        classes=classes,
    )


def _bool_cfg(cfg: dict, chave: str, padrao: str = "nao") -> bool:
    return str(cfg.get(chave, padrao)).strip().lower() in ("sim", "true", "1", "yes")


def _criar_detector_placa(cfg: dict, modelo_oim: str | None = None):
    """Só o detector de PLACA, sem estágio de veículo nem varredura em janelas.

    Extraído de `criar_detector` porque `obter_detector_leitura` precisa exatamente disto
    e antes chamava `criar_detector(cfg)` — que já aplica `veiculo_dois_estagios_live` e
    portanto podia devolver um `DetectorDoisEstagios` pronto. Aquele resultado era então
    envolvido em OUTRO `DetectorDoisEstagios` e, pior, entregue ao `BuscaEmTiles` como
    `detector_tiles`: cada janela passava a rodar o estágio de veículo, exatamente o que o
    comentário de `BuscaEmTiles` diz que nunca deve acontecer.

    `modelo_oim` sobrescreve o modelo do backend open-image-models (a leitura GET usa um
    modelo próprio, maior).
    """
    conf = float(cfg.get("conf_threshold", "0.3"))
    nms = float(cfg.get("nms_threshold", "0.4"))
    backend = (cfg.get("detector_backend", "open_image_models") or "open_image_models").strip().lower()

    if backend == "open_image_models":
        return OpenImageDetector(
            modelo=modelo_oim or cfg.get("oim_modelo", "yolo-v9-t-384-license-plate-end2end"),
            conf=conf,
        )
    extras = [e.strip() for e in cfg.get("detector_modelos_extra", "").split(",") if e.strip()]
    if extras:
        dets = [Detector(cfg["modelo_path"], conf, nms)]
        for m in extras:
            dets.append(Detector(m, conf, nms))
        return MultiDetector(dets, votos_minimos=max(1, int(cfg.get("detector_votos_minimos", "1"))))
    return Detector(modelo_path=cfg["modelo_path"], conf=conf, nms=nms)


def criar_detector(cfg: dict):
    """Fábrica de detector conforme a config. Usada por pipeline e endpoint ler-placa.

    detector_backend:
      - "open_image_models" (padrão): YOLOv9-t MIT via open-image-models (comercial-OK)
      - "onnx": modelo ONNX local (models/*.onnx) — suporta votação MultiDetector

    Se `veiculo_dois_estagios_live=sim`, envolve o detector de placa com um estágio de
    detecção de veículo (VehicleDetector) — ver `DetectorDoisEstagios`.
    """
    detector_placa = _criar_detector_placa(cfg)

    if _bool_cfg(cfg, "veiculo_dois_estagios_live"):
        return DetectorDoisEstagios(
            detector_placa, _criar_detector_veiculo(cfg),
            padding=float(cfg.get("veiculo_padding", "0.05")),
            obrigatorio=_bool_cfg(cfg, "veiculo_obrigatorio"),
            max_veiculos=int(cfg.get("veiculo_max_veiculos", "5")),
        )
    return detector_placa


# Detector dedicado à leitura sob demanda (botão "Ler Placa"/GET) — cacheado por modelo.
_detector_leitura = None
_detector_leitura_id: tuple | None = None

# Protege chamadas concorrentes ao detector cacheado acima. FastAPI roda cada request
# num thread do pool — com 2+ câmeras, dois cliques simultâneos de "Ler Placa" chamariam
# .detectar() na MESMA sessão onnxruntime de threads diferentes. Em CPUExecutionProvider
# isso é seguro, mas em CUDAExecutionProvider (GPU) NÃO é — pode travar ou crashar
# (bug conhecido do onnxruntime com handles cuDNN compartilhados entre threads). O lock
# serializa o acesso: sem paralelismo entre leituras concorrentes, mas sem risco de crash.
detector_leitura_lock = threading.Lock()

# Protege a CRIAÇÃO do detector cacheado acima (diferente do lock acima, que protege o
# USO). Sem isso, duas requisições concorrentes vendo `_detector_leitura is None` ao
# mesmo tempo (ex.: logo após o boot, antes do aquecimento terminar) carregam a pilha de
# modelo cada uma a sua própria vez — carga duplicada, e em GPU risco real de sessões
# CUDA sendo inicializadas concorrentemente.
_detector_leitura_criacao_lock = threading.Lock()


def obter_detector_leitura(cfg: dict):
    """Detector de alta precisão para a leitura manual/GET (padrão: s-608).

    Carregado uma vez e reusado. O stream ao vivo continua com o detector do pipeline
    (mais leve); aqui priorizamos precisão porque o GET tolera mais latência.

    Se `veiculo_dois_estagios_get=sim`, envolve com um estágio de detecção de veículo
    (ver `DetectorDoisEstagios`) — vale a latência extra porque é sob demanda.
    """
    backend = (cfg.get("detector_backend", "open_image_models") or "").strip().lower()
    if backend == "open_image_models":
        modelo = cfg.get("oim_modelo_leitura") or cfg.get("oim_modelo", "yolo-v9-t-512-license-plate-end2end")
        ident_placa = ("oim", modelo)
    else:
        # Backend ONNX local: reusa a fábrica normal (sem modelo dedicado de leitura)
        ident_placa = ("onnx", cfg.get("modelo_path", ""))

    dois_estagios = _bool_cfg(cfg, "veiculo_dois_estagios_get", "sim")
    tiles = _bool_cfg(cfg, "tiles_fallback_get", "sim")
    # A identidade tem de cobrir TODA chave que a fábrica congela na construção — este
    # detector é um singleton cacheado, e nada mais o invalida quando a config é salva. O
    # `ident` antigo listava só os modelos e os dois booleanos, então mexer em
    # `conf_threshold`, `veiculo_conf`, `veiculo_obrigatorio`, `veiculo_max_veiculos` (ou
    # qualquer um dos demais abaixo) não tinha efeito nenhum no "Ler Placa" até o processo
    # reiniciar — enquanto o stream ao vivo, que é recriado por `pipeline.reiniciar`, pegava
    # a mudança na hora. O diagnóstico invertia: o botão parecia ignorar o ajuste e o
    # caminho que já estava bom parecia responder. Espelha `obter_ocr_leitura`, cujo ident
    # inclui todos os parâmetros que ele passa.
    ident = (
        *ident_placa,
        cfg.get("conf_threshold", ""), cfg.get("nms_threshold", ""),
        cfg.get("detector_modelos_extra", ""), cfg.get("detector_votos_minimos", ""),
        dois_estagios,
        (cfg.get("veiculo_modelo_path", ""), cfg.get("veiculo_conf", ""),
         cfg.get("veiculo_nms", ""), cfg.get("veiculo_classes", ""),
         cfg.get("veiculo_padding", ""), cfg.get("veiculo_obrigatorio", ""),
         cfg.get("veiculo_max_veiculos", "")) if dois_estagios else "",
        tiles,
        (cfg.get("tiles_lado_alvo", ""), cfg.get("tiles_conf", ""),
         cfg.get("tiles_sobreposicao", ""), cfg.get("tiles_max_janelas", "")) if tiles else "",
    )

    def _construir():
        # SÓ o detector de placa: envolver os estágios é responsabilidade daqui
        # para baixo. `criar_detector(cfg)` devolveria um 2 estágios já montado
        # (ele aplica `veiculo_dois_estagios_live`), que seria envolvido de novo.
        det_placa = _criar_detector_placa(
            cfg, modelo_oim=ident_placa[1] if backend == "open_image_models" else None)

        if dois_estagios:
            det = DetectorDoisEstagios(
                det_placa, _criar_detector_veiculo(cfg),
                padding=float(cfg.get("veiculo_padding", "0.05")),
                obrigatorio=_bool_cfg(cfg, "veiculo_obrigatorio"),
                max_veiculos=int(cfg.get("veiculo_max_veiculos", "5")),
            )
        else:
            det = det_placa

        # Sempre por FORA do 2 estágios: as janelas só devem ser varridas quando o
        # caminho normal inteiro (veículo→placa, com fallback no recorte todo) não
        # achou nada. Por dentro, cada recorte de veículo dispararia sua própria
        # varredura — latência multiplicada sem motivo.
        if tiles:
            # Detector próprio para as janelas quando `tiles_conf` for mais
            # permissivo que o principal — é uma segunda sessão do MESMO modelo,
            # só com outro limiar (o open-image-models fixa o limiar na
            # construção, não aceita por chamada). Igual ou maior, reusa o
            # principal e não gasta memória à toa.
            conf_tiles = float(cfg.get("tiles_conf", "0.15"))
            if backend == "open_image_models" and conf_tiles < float(cfg.get("conf_threshold", "0.3")):
                det_tiles = OpenImageDetector(modelo=ident_placa[1], conf=conf_tiles)
            else:
                det_tiles = det_placa
            det = BuscaEmTiles(
                det, det_tiles,
                lado_alvo=int(cfg.get("tiles_lado_alvo", "300")),
                sobreposicao=float(cfg.get("tiles_sobreposicao", "0.30")),
                max_janelas=int(cfg.get("tiles_max_janelas", "6")),
            )

        det.carregar()
        log.info("Detector de leitura (GET) carregado: %s (2 estágios=%s, tiles=%s)",
                 ident_placa[1], dois_estagios, tiles)
        return det

    def _definir(v, i):
        global _detector_leitura, _detector_leitura_id
        _detector_leitura, _detector_leitura_id = v, i

    return _fab.resolver(lambda: (_detector_leitura, _detector_leitura_id), ident,
                         _detector_leitura_criacao_lock, _construir, _definir)


# Detector do perfil RÁPIDO (`GET /api/leitura?rapido=1`) — slots PRÓPRIOS, e não um
# `cfg` alterado passado para `obter_detector_leitura`. Aquela fábrica tem um slot global
# único indexado por `ident`: chamá-la com outra config DESPEJA a instância de alta
# precisão e a recarrega na chamada seguinte. Alternar rápido/completo faria thrashing de
# modelo — dezenas de segundos por alternância, exatamente o custo que este perfil existe
# para evitar.
_detector_rapido = None
_detector_rapido_id: tuple | None = None

# Lock de USO próprio, e não o `detector_leitura_lock`: compartilhar serializaria uma
# leitura rápida atrás de uma completa que pode levar 28s — o pior caso possível para o
# modo cuja razão de existir é responder rápido. Mesma motivação de crash do lock de
# leitura (sessão onnxruntime compartilhada entre threads do pool do FastAPI).
detector_rapido_lock = threading.Lock()
_detector_rapido_criacao_lock = threading.Lock()


def obter_detector_rapido(cfg: dict):
    """Detector do perfil rápido: o MESMO que o stream ao vivo usa, cacheado à parte.

    O corpo delega a `criar_detector` de propósito — é literalmente a fábrica do pipeline
    contínuo (app/visao/pipeline.py:__init__). Assim o perfil rápido não é "mais um
    conjunto de decisões de modelo" que alguém precisa manter sincronizado: é o perfil ao
    vivo, e muda junto com ele. Consequências que vêm de graça daí: modelo t-512 em vez de
    s-608, `veiculo_dois_estagios_live` no lugar de `..._get`, e nenhuma `BuscaEmTiles`
    (a varredura em janelas custa até 6 passadas extras de detector).

    O que se perde em relação ao completo está medido no histórico do projeto: a varredura
    em janelas é o que fez moto sair de 0/12 para 12/12. Placa de moto distante NÃO vai
    ser lida aqui — é o preço declarado do modo.
    """
    # A identidade tem de cobrir TODA config que `criar_detector` congela na construção.
    # Ident incompleto já custou caro neste projeto: ajuste salvo, confirmado na tela, e
    # que nunca chegava ao detector cacheado até o processo reiniciar (ver o comentário
    # em `obter_detector_leitura`). A lista abaixo espelha `_criar_detector_placa` +
    # `_criar_detector_veiculo` + o ramo de 2 estágios de `criar_detector`.
    dois_estagios = _bool_cfg(cfg, "veiculo_dois_estagios_live")
    ident = (
        cfg.get("detector_backend", ""),
        cfg.get("oim_modelo", ""),
        cfg.get("modelo_path", ""),
        cfg.get("conf_threshold", ""), cfg.get("nms_threshold", ""),
        cfg.get("detector_modelos_extra", ""), cfg.get("detector_votos_minimos", ""),
        dois_estagios,
        (cfg.get("veiculo_modelo_path", ""), cfg.get("veiculo_conf", ""),
         cfg.get("veiculo_nms", ""), cfg.get("veiculo_classes", ""),
         cfg.get("veiculo_padding", ""), cfg.get("veiculo_obrigatorio", ""),
         cfg.get("veiculo_max_veiculos", "")) if dois_estagios else "",
    )

    def _construir():
        det = criar_detector(cfg)
        det.carregar()
        log.info("Detector rápido carregado: %s (2 estágios=%s, sem tiles)",
                 cfg.get("oim_modelo", cfg.get("modelo_path", "?")), dois_estagios)
        return det

    def _definir(v, i):
        global _detector_rapido, _detector_rapido_id
        _detector_rapido, _detector_rapido_id = v, i

    return _fab.resolver(lambda: (_detector_rapido, _detector_rapido_id), ident,
                         _detector_rapido_criacao_lock, _construir, _definir)
