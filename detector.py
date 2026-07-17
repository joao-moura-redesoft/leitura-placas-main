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


def criar_detector(cfg: dict):
    """Fábrica de detector conforme a config. Usada por pipeline e endpoint ler-placa.

    detector_backend:
      - "open_image_models" (padrão): YOLOv9-t MIT via open-image-models (comercial-OK)
      - "onnx": modelo ONNX local (models/*.onnx) — suporta votação MultiDetector
    """
    conf = float(cfg.get("conf_threshold", "0.3"))
    nms = float(cfg.get("nms_threshold", "0.4"))
    backend = (cfg.get("detector_backend", "open_image_models") or "open_image_models").strip().lower()

    if backend == "open_image_models":
        return OpenImageDetector(
            modelo=cfg.get("oim_modelo", "yolo-v9-t-384-license-plate-end2end"),
            conf=conf,
        )

    extras = [e.strip() for e in cfg.get("detector_modelos_extra", "").split(",") if e.strip()]
    if extras:
        dets = [Detector(cfg["modelo_path"], conf, nms)]
        for m in extras:
            dets.append(Detector(m, conf, nms))
        return MultiDetector(dets, votos_minimos=max(1, int(cfg.get("detector_votos_minimos", "1"))))
    return Detector(modelo_path=cfg["modelo_path"], conf=conf, nms=nms)


# Detector dedicado à leitura sob demanda (botão "Ler Placa"/GET) — cacheado por modelo.
_detector_leitura = None
_detector_leitura_id: tuple | None = None


def obter_detector_leitura(cfg: dict):
    """Detector de alta precisão para a leitura manual/GET (padrão: s-608).

    Carregado uma vez e reusado. O stream ao vivo continua com o detector do pipeline
    (mais leve); aqui priorizamos precisão porque o GET tolera mais latência.
    """
    global _detector_leitura, _detector_leitura_id
    backend = (cfg.get("detector_backend", "open_image_models") or "").strip().lower()
    if backend == "open_image_models":
        modelo = cfg.get("oim_modelo_leitura") or cfg.get("oim_modelo", "yolo-v9-t-512-license-plate-end2end")
        ident = ("oim", modelo)
    else:
        # Backend ONNX local: reusa a fábrica normal (sem modelo dedicado de leitura)
        ident = ("onnx", cfg.get("modelo_path", ""))

    if _detector_leitura is None or _detector_leitura_id != ident:
        if backend == "open_image_models":
            det = OpenImageDetector(modelo=ident[1], conf=float(cfg.get("conf_threshold", "0.3")))
        else:
            det = criar_detector(cfg)
        det.carregar()
        _detector_leitura = det
        _detector_leitura_id = ident
        log.info("Detector de leitura (GET) carregado: %s", ident[1])
    return _detector_leitura
