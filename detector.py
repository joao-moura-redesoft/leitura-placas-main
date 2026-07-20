"""Detector de placas via ONNX Runtime.

Suporta dois formatos de saída automaticamente:
  - YOLO26 end-to-end (padrão): (N, 300, 6) → xyxy + conf + cls, sem NMS
  - YOLOv8/v9/v10/v11:         (N, nc+4, 8400) → cxcywh + classes, requer NMS

Quando o modelo não está disponível, faz fallback para detecção por contornos
(menos preciso, útil em desenvolvimento sem o .onnx baixado).
"""
from __future__ import annotations
import logging
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)

INPUT_SIZE = 640


class Detector:
    def __init__(self, modelo_path: str, conf: float = 0.5, nms: float = 0.4):
        self.modelo_path = Path(modelo_path)
        self.conf = conf
        self.nms = nms
        self.sess = None
        self.input_name: str | None = None

    def carregar(self) -> None:
        if not self.modelo_path.exists():
            log.warning("Modelo %s não encontrado — usando fallback por contornos", self.modelo_path)
            return
        try:
            import onnxruntime as ort
            providers = ["CPUExecutionProvider"]
            self.sess = ort.InferenceSession(str(self.modelo_path), providers=providers)
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

    def carregar(self) -> None:
        try:
            from open_image_models import LicensePlateDetector
        except ImportError:
            log.error("open-image-models não instalado — rode: pip install open-image-models")
            self.sess = None
            return
        try:
            self._det = LicensePlateDetector(detection_model=self.modelo, conf_thresh=self.conf)
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
            log.warning("open-image-models: falha na inferência (%s)", e)
            return []
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

    def __init__(self, modelo_path: str, conf: float = 0.4, nms: float = 0.5,
                 classes: frozenset[int] | None = None):
        self.modelo_path = Path(modelo_path)
        self.conf = conf
        self.nms = nms
        self.classes = classes if classes is not None else frozenset(self.CLASSES_COCO)
        self.sess = None
        self.input_name: str | None = None
        self._grids, self._strides_exp = self._gerar_grids()

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
        if not self.modelo_path.exists():
            log.warning("VehicleDetector: modelo %s não encontrado — 2 estágios cairá "
                        "sempre no fallback (frame inteiro)", self.modelo_path)
            return
        try:
            import onnxruntime as ort
            self.sess = ort.InferenceSession(str(self.modelo_path), providers=["CPUExecutionProvider"])
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

    def detectar(self, frame) -> list[tuple[int, int, int, int, float]]:
        """Retorna bboxes de veículo (x,y,w,h,conf) em coordenadas do frame original."""
        if self.sess is None or frame is None or frame.size == 0:
            return []
        try:
            return self._inferir(frame)
        except Exception as e:
            log.warning("VehicleDetector: falha na inferência (%s)", e)
            return []

    def _inferir(self, frame) -> list[tuple[int, int, int, int, float]]:
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
                resultado.append((x0, y0, x1 - x0, y1 - y0, float(s[i])))
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
            return self.detector_placa.detectar(frame)   # fallback seguro: frame inteiro

        if len(veiculos) > self.max_veiculos:
            veiculos = sorted(veiculos, key=lambda v: v[2] * v[3], reverse=True)[: self.max_veiculos]

        f_h, f_w = frame.shape[:2]
        placas: list[tuple[int, int, int, int, float]] = []
        for vx, vy, vw, vh, _vconf in veiculos:
            dx, dy = int(vw * self.padding), int(vh * self.padding)
            x0, y0 = max(0, vx - dx), max(0, vy - dy)
            x1, y1 = min(f_w, vx + vw + dx), min(f_h, vy + vh + dy)
            crop = frame[y0:y1, x0:x1]
            if crop.size == 0:
                continue
            for px, py, pw, ph, pconf in self.detector_placa.detectar(crop):
                placas.append((px + x0, py + y0, pw, ph, pconf))

        if len(veiculos) > 1 and len(placas) > 1:
            placas = self._dedup(placas)
        return placas

    @staticmethod
    def _dedup(placas: list[tuple[int, int, int, int, float]]) -> list[tuple[int, int, int, int, float]]:
        """Remove placas duplicadas de veículos sobrepostos (mantém a de maior confiança)."""
        placas = sorted(placas, key=lambda p: -p[4])
        mantidas: list[tuple[int, int, int, int, float]] = []
        for p in placas:
            if all(MultiDetector._iou(p, m) < 0.5 for m in mantidas):
                mantidas.append(p)
        return mantidas


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


def criar_detector(cfg: dict):
    """Fábrica de detector conforme a config. Usada por pipeline e endpoint ler-placa.

    detector_backend:
      - "open_image_models" (padrão): YOLOv9-t MIT via open-image-models (comercial-OK)
      - "onnx": modelo ONNX local (models/*.onnx) — suporta votação MultiDetector

    Se `veiculo_dois_estagios_live=sim`, envolve o detector de placa com um estágio de
    detecção de veículo (VehicleDetector) — ver `DetectorDoisEstagios`.
    """
    conf = float(cfg.get("conf_threshold", "0.3"))
    nms = float(cfg.get("nms_threshold", "0.4"))
    backend = (cfg.get("detector_backend", "open_image_models") or "open_image_models").strip().lower()

    if backend == "open_image_models":
        detector_placa = OpenImageDetector(
            modelo=cfg.get("oim_modelo", "yolo-v9-t-384-license-plate-end2end"),
            conf=conf,
        )
    else:
        extras = [e.strip() for e in cfg.get("detector_modelos_extra", "").split(",") if e.strip()]
        if extras:
            dets = [Detector(cfg["modelo_path"], conf, nms)]
            for m in extras:
                dets.append(Detector(m, conf, nms))
            detector_placa = MultiDetector(dets, votos_minimos=max(1, int(cfg.get("detector_votos_minimos", "1"))))
        else:
            detector_placa = Detector(modelo_path=cfg["modelo_path"], conf=conf, nms=nms)

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


def obter_detector_leitura(cfg: dict):
    """Detector de alta precisão para a leitura manual/GET (padrão: s-608).

    Carregado uma vez e reusado. O stream ao vivo continua com o detector do pipeline
    (mais leve); aqui priorizamos precisão porque o GET tolera mais latência.

    Se `veiculo_dois_estagios_get=sim`, envolve com um estágio de detecção de veículo
    (ver `DetectorDoisEstagios`) — vale a latência extra porque é sob demanda.
    """
    global _detector_leitura, _detector_leitura_id
    backend = (cfg.get("detector_backend", "open_image_models") or "").strip().lower()
    if backend == "open_image_models":
        modelo = cfg.get("oim_modelo_leitura") or cfg.get("oim_modelo", "yolo-v9-t-512-license-plate-end2end")
        ident_placa = ("oim", modelo)
    else:
        # Backend ONNX local: reusa a fábrica normal (sem modelo dedicado de leitura)
        ident_placa = ("onnx", cfg.get("modelo_path", ""))

    dois_estagios = _bool_cfg(cfg, "veiculo_dois_estagios_get", "sim")
    ident = (*ident_placa, dois_estagios, cfg.get("veiculo_modelo_path", "") if dois_estagios else "")

    if _detector_leitura is None or _detector_leitura_id != ident:
        if backend == "open_image_models":
            det_placa = OpenImageDetector(modelo=ident_placa[1], conf=float(cfg.get("conf_threshold", "0.3")))
        else:
            det_placa = criar_detector(cfg)

        if dois_estagios:
            det = DetectorDoisEstagios(
                det_placa, _criar_detector_veiculo(cfg),
                padding=float(cfg.get("veiculo_padding", "0.05")),
                obrigatorio=_bool_cfg(cfg, "veiculo_obrigatorio"),
                max_veiculos=int(cfg.get("veiculo_max_veiculos", "5")),
            )
        else:
            det = det_placa

        det.carregar()
        _detector_leitura = det
        _detector_leitura_id = ident
        log.info("Detector de leitura (GET) carregado: %s (2 estágios=%s)", ident_placa[1], dois_estagios)
    return _detector_leitura
