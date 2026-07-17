"""OCR — múltiplos engines com auto-instalação via pip.

Engines suportados:
  tesseract     : padrão, leve, requer binário Tesseract instalado no SO
  easyocr       : deep learning, bom para placas, pesado (~200 MB PyTorch)
  paddleocr     : deep learning, mais leve que EasyOCR, ótimo para ARM64
  doctr         : transformers (Mindee), bom para alfanuméricos
  fast_plate_ocr: ONNX específico para placas, bem leve

Engines não instalados são detectados automaticamente e instalados via
pip na primeira inicialização. Em caso de falha, o sistema cai para tesseract.
"""
from __future__ import annotations
import importlib
import logging
import os
import re
import shutil
import subprocess
import sys

import cv2
import numpy as np

import estado

log = logging.getLogger(__name__)

CHARS_VALIDOS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _ordenar_pontos(pts: np.ndarray) -> np.ndarray:
    """Ordena 4 pontos: [top-left, top-right, bottom-right, bottom-left]."""
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array([
        pts[np.argmin(s)],
        pts[np.argmin(d)],
        pts[np.argmax(s)],
        pts[np.argmax(d)],
    ], dtype=np.float32)

TESSERACT_WIN_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
]

# Pacotes pip necessários para cada engine
_ENGINE_PACOTES: dict[str, list[str]] = {
    "easyocr": ["easyocr"],
    "paddleocr": ["paddlepaddle", "paddleocr"],
    "doctr": ["python-doctr[torch]"],
    "fast_plate_ocr": ["fast-plate-ocr"],
}

# Módulo Python usado para verificar se o engine está disponível
_ENGINE_MODULO: dict[str, str] = {
    "easyocr": "easyocr",
    "paddleocr": "paddleocr",
    "doctr": "doctr",
    "fast_plate_ocr": "fast_plate_ocr",
}


def _localizar_tesseract() -> str | None:
    no_path = shutil.which("tesseract")
    if no_path:
        return no_path
    for p in TESSERACT_WIN_PATHS:
        if p and os.path.isfile(p):
            return p
    return None


def _auto_instalar(engine: str) -> bool:
    """Instala os pacotes pip necessários para o engine. Retorna True se OK."""
    pacotes = _ENGINE_PACOTES.get(engine, [])
    if not pacotes:
        return False
    log.info("Engine '%s' não instalado — iniciando instalação: %s", engine, " ".join(pacotes))
    for pacote in pacotes:
        log.info("  pip install %s ...", pacote)
        estado.instalando_pacote = pacote
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", pacote, "--quiet"],
                timeout=300,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            log.error("Falha ao instalar '%s': %s", pacote, e)
            estado.instalando_pacote = ""
            return False
    estado.instalando_pacote = ""
    log.info("Engine '%s' instalado com sucesso", engine)
    return True


def _tentar_importar(engine: str) -> bool:
    """Tenta importar o módulo do engine; em falha, instala e tenta de novo."""
    modulo = _ENGINE_MODULO.get(engine)
    if not modulo:
        return False
    try:
        importlib.import_module(modulo)
        return True
    except ImportError:
        if _auto_instalar(engine):
            try:
                importlib.import_module(modulo)
                return True
            except ImportError:
                log.error("Módulo '%s' indisponível mesmo após instalação", modulo)
    return False


class OCR:
    def __init__(self, engine: str = "tesseract", tesseract_psm: int = 7):
        self.engine = engine
        self.psm = tesseract_psm
        self._easyocr_reader = None
        self._paddle = None
        self._doctr = None
        self._fast_plate = None

    def carregar(self) -> None:
        engine = self.engine
        if engine == "easyocr":
            self._carregar_easyocr()
        elif engine == "paddleocr":
            self._carregar_paddleocr()
        elif engine == "doctr":
            self._carregar_doctr()
        elif engine == "fast_plate_ocr":
            self._carregar_fast_plate_ocr()
        else:
            self._carregar_tesseract()

    # -- Carregamento ----------------------------------------------------------

    def _carregar_tesseract(self) -> None:
        try:
            import pytesseract
        except ImportError:
            log.error("pytesseract não instalado — execute: pip install pytesseract")
            return
        caminho = _localizar_tesseract()
        if caminho:
            pytesseract.pytesseract.tesseract_cmd = caminho
            log.info("Tesseract localizado em %s", caminho)
        else:
            log.warning(
                "Binário Tesseract não encontrado. "
                "Windows: winget install UB-Mannheim.TesseractOCR | "
                "Linux: apt install tesseract-ocr"
            )

    def _carregar_easyocr(self) -> None:
        if not _tentar_importar("easyocr"):
            log.error("EasyOCR indisponível — caindo para tesseract")
            self.engine = "tesseract"
            self._carregar_tesseract()
            return
        import easyocr
        self._easyocr_reader = easyocr.Reader(["en"], gpu=False, verbose=False)
        log.info("EasyOCR carregado")
        # Warm-up: força JIT do PyTorch para compilar os kernels durante o startup
        # e não na primeira leitura real (o que causaria atraso de 3-8 segundos).
        try:
            self._easyocr_reader.readtext(np.zeros((50, 200, 3), dtype=np.uint8))
            log.info("EasyOCR aquecido")
        except Exception:
            pass

    def _carregar_paddleocr(self) -> None:
        if not _tentar_importar("paddleocr"):
            log.error("PaddleOCR indisponível — caindo para tesseract")
            self.engine = "tesseract"
            self._carregar_tesseract()
            return
        from paddleocr import PaddleOCR
        self._paddle = PaddleOCR(use_angle_cls=False, lang="en", use_gpu=False, show_log=False)
        log.info("PaddleOCR carregado")

    def _carregar_doctr(self) -> None:
        if not _tentar_importar("doctr"):
            log.error("docTR indisponível — caindo para tesseract")
            self.engine = "tesseract"
            self._carregar_tesseract()
            return
        from doctr.models import ocr_predictor
        self._doctr = ocr_predictor(pretrained=True)
        log.info("docTR carregado")

    def _carregar_fast_plate_ocr(self) -> None:
        if not _tentar_importar("fast_plate_ocr"):
            log.error("fast-plate-ocr indisponível — caindo para tesseract")
            self.engine = "tesseract"
            self._carregar_tesseract()
            return
        from fast_plate_ocr import LicensePlateRecognizer
        self._fast_plate = LicensePlateRecognizer("global-plates-mobile-vit-v2-model")
        log.info("fast-plate-ocr carregado")
        # Warm-up: ONNX Runtime otimiza o grafo na primeira execução — fazer no startup.
        try:
            self._fast_plate.run(np.zeros((50, 200), dtype=np.uint8))
            log.info("fast-plate-ocr aquecido")
        except Exception:
            pass

    # -- Pré-processamento -----------------------------------------------------

    def _corrigir_perspectiva(self, crop_bgr) -> np.ndarray:
        """Detecta o quadrilátero da placa e aplica transformada de perspectiva.

        Adiciona margem preta ao redor do crop para ajudar a detectar a borda
        da placa quando o YOLO cortou rente aos caracteres. Retorna o crop
        original sem modificação se nenhum quadrilátero adequado for encontrado.

        Dimensões de saída (proporções oficiais DENATRAN):
          - Carro/moto-carro: 400×130 px (razão ≈ 3.08:1)
          - Moto:             200×140 px (razão ≈ 1.43:1)
        Detectado pela razão largura/altura do crop recebido.
        """
        if crop_bgr.ndim != 3 or crop_bgr.size == 0:
            return crop_bgr
        h, w = crop_bgr.shape[:2]
        if h < 20 or w < 20:
            return crop_bgr

        # Margem preta — ajuda a encontrar a borda da placa quando o crop é rente
        pad = max(10, int(min(h, w) * 0.08))
        padded = cv2.copyMakeBorder(crop_bgr, pad, pad, pad, pad,
                                    cv2.BORDER_CONSTANT, value=(0, 0, 0))
        ph, pw = padded.shape[:2]

        cinza = cv2.cvtColor(padded, cv2.COLOR_BGR2GRAY)
        borrado = cv2.GaussianBlur(cinza, (5, 5), 0)

        med = float(np.median(borrado))
        lower = int(max(0, 0.67 * med))
        upper = int(min(255, 1.33 * med))
        bordas = cv2.Canny(borrado, lower, upper)

        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        bordas = cv2.dilate(bordas, kernel, iterations=1)

        contornos, _ = cv2.findContours(bordas, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contornos:
            return crop_bgr

        contornos = sorted(contornos, key=cv2.contourArea, reverse=True)[:5]
        area_min = ph * pw * 0.20

        for cnt in contornos:
            if cv2.contourArea(cnt) < area_min:
                continue
            peri = cv2.arcLength(cnt, True)
            approx = cv2.approxPolyDP(cnt, 0.04 * peri, True)
            if len(approx) != 4:
                continue

            pts = approx.reshape(4, 2).astype(np.float32)
            pts_ord = _ordenar_pontos(pts)

            # Proporção do crop original determina o tipo de placa
            if (w / max(h, 1)) > 2.0:
                w_dst, h_dst = 400, 130   # carro
            else:
                w_dst, h_dst = 200, 140   # moto

            # Preserva resolução para crops pequenos: se a imagem original for
            # maior que o alvo mas menor que 1500px, escala o destino para não
            # comprimir a imagem e degradar OCR (ex: foto real de placa moto).
            # Para imagens grandes (>= 1500px) aplica o warp padrão, que extrai
            # apenas a região da placa na resolução correta.
            if max(w, h) < 1500:
                scale = max(w / w_dst, h / h_dst)
                if scale > 1.0:
                    w_dst = int(w_dst * scale)
                    h_dst = int(h_dst * scale)

            dst = np.array(
                [[0, 0], [w_dst - 1, 0], [w_dst - 1, h_dst - 1], [0, h_dst - 1]],
                dtype=np.float32,
            )
            M = cv2.getPerspectiveTransform(pts_ord, dst)
            warped = cv2.warpPerspective(padded, M, (w_dst, h_dst))
            log.debug("Perspectiva corrigida %dx%d → %dx%d", w, h, w_dst, h_dst)
            return warped

        return crop_bgr

    def _remover_header(self, crop_bgr) -> tuple[np.ndarray, bool, bool]:
        """Remove o cabeçalho da placa Mercosul (carro e moto).

        Retorna: (crop_sem_header, tinha_header, e_mercosul)
          - tinha_header : True se qualquer faixa superior foi removida
          - e_mercosul   : True apenas se o header tinha cor saturada (azul/vermelho)
                           False para faixas cinzas/metálicas de placas antigas

        Três métodos em cascata:

        Método 1 — Brilho (placas Mercosul carro):
          Encontra primeira linha com brilho > 180 (área branca abaixo do header).
          Depois verifica saturação da região acima para confirmar se é Mercosul.

        Método 1b — Salto de brilho (placa moto antigo com faixa metálica):
          Quando Method 1 encontra brilho > 180 já na linha 0 (fundo prateado),
          procura a transição para o branco puro da placa (brilho[0]+25 e > 195).
          Aplica-se apenas a crops moto-like (razão ≤ 2.5).

        Método 2 — Saturação HSV (placas Mercosul moto):
          Detecta o header pelo alto valor de saturação (azul/vermelho Mercosul).
          Sempre retorna e_mercosul=True pois detecta explicitamente cor saturada.
        """
        if crop_bgr.ndim != 3 or crop_bgr.shape[0] < 24:
            return crop_bgr, False, False

        h, w = crop_bgr.shape[:2]
        cx0, cx1 = int(w * 0.35), int(w * 0.65)
        regiao = crop_bgr[: int(h * 0.50), cx0:cx1]
        if regiao.size == 0:
            return crop_bgr, False, False

        # -- Método 1: brilho (carro) ------------------------------------------
        cinza = cv2.cvtColor(regiao, cv2.COLOR_BGR2GRAY)
        borrado = cv2.GaussianBlur(cinza, (1, 11), 0)
        brilho = borrado.mean(axis=1)

        corte_local = -1
        for i, b in enumerate(brilho):
            if b > 180:
                corte_local = i
                break

        if corte_local > int(h * 0.05):
            corte = min(corte_local + int(h * 0.02), int(h * 0.50))
            # Confirma se o header acima do corte tem cor saturada (Mercosul azul)
            # Placas antigas têm faixa cinza/metálica — saturação < 30
            header_region = regiao[:corte_local, :]
            if header_region.size > 0:
                hsv_h = cv2.cvtColor(header_region, cv2.COLOR_BGR2HSV)
                sat_media = float(hsv_h[:, :, 1].mean())
                e_mercosul = sat_media > 30
            else:
                sat_media = 0.0
                e_mercosul = False
            log.info(
                "_remover_header M1: corte=%d/%d mercosul=%s sat=%.0f",
                corte, h, e_mercosul, sat_media,
            )
            return crop_bgr[corte:, :], True, e_mercosul

        # -- Método 1b: faixa metálica de placa moto antigo --------------------
        # corte_local == 0 significa que brilho[0] > 180 (fundo prateado do topo).
        # Procura a transição para a área branca da placa (ainda mais brilhante).
        # A condição brilho[0] <= 215 exclui crops que já começam no branco puro.
        if (corte_local == 0
                and w / max(h, 1) <= 2.5
                and len(brilho) > 10
                and brilho[0] <= 215):
            for i in range(int(h * 0.10), min(len(brilho), int(h * 0.42))):
                if brilho[i] > brilho[0] + 25 and brilho[i] > 195:
                    corte = min(i + max(1, int(h * 0.02)), int(h * 0.45))
                    log.debug(
                        "Header antigo metálico: linha %d brilho %.0f→%.0f corte %d",
                        i, brilho[0], brilho[i], corte,
                    )
                    return crop_bgr[corte:, :], True, False

        # -- Método 2: saturação HSV (moto) ------------------------------------
        # Detecta faixa com cor saturada no topo e verifica que a matiz é
        # azul/verde (H 80-140 OpenCV), excluindo pele (H 5-20) e outros ruídos.
        hsv = cv2.cvtColor(regiao, cv2.COLOR_BGR2HSV)
        sat = hsv[:, :, 1].mean(axis=1)

        inicio = next((i for i, s in enumerate(sat) if s > 40), -1)
        if inicio < 0:
            return crop_bgr, False, False

        ultimo_header = -1
        linhas_fora = 0
        for i in range(inicio, len(sat)):
            if sat[i] > 50:
                ultimo_header = i
                linhas_fora = 0
            else:
                linhas_fora += 1
                if linhas_fora > 4:
                    break

        # Header Mercosul: começa perto do topo (< 15%), tem pelo menos 3 linhas e termina antes de 42%
        if not (inicio < int(h * 0.15)
                and (ultimo_header - inicio) >= 2
                and int(h * 0.03) < ultimo_header < int(h * 0.42)):
            return crop_bgr, False, False

        # Confirma que a cor é azul/verde Mercosul e não pele/laranja/outro ruído.
        # OpenCV H: azul ≈ 100-130, verde ≈ 55-85; pele ≈ 5-20.
        faixa_hue = hsv[inicio : ultimo_header + 1, :, 0]
        hue_media = float(faixa_hue.mean()) if faixa_hue.size > 0 else 0
        if not (50 <= hue_media <= 150):
            log.info(
                "_remover_header M2: saturação detectada mas matiz=%.0f fora do range azul/verde"
                " (50-150) — ignorando (provável pele ou fundo colorido)",
                hue_media,
            )
            return crop_bgr, False, False

        corte = min(ultimo_header + int(h * 0.03) + 1, int(h * 0.45))
        log.debug(
            "Header detectado via saturação: linha %d, corte em %d, hue=%.0f",
            ultimo_header, corte, hue_media,
        )
        return crop_bgr[corte:, :], True, True

    def _remover_ruidos_mercosul(self, crop) -> np.ndarray:
        """Apaga QR code e marcador 'BR' da área branca após remoção do header.

        Carro  (proporção ~3:1):
          - QR code CRLV-e : 14% largura × 38% altura (canto sup. esq.)
          - Marcador "BR"  : 12% largura × 30% altura (canto inf. esq.)

        Moto (proporção ~1.4:1 — caracteres maiores, BR proporcionalmente maior):
          - QR code         : 20% largura × 28% altura (canto sup. esq.)
          - Marcador "BR"   : 18% largura × 30% altura (canto inf. esq.)

        Usa a proporção largura/altura do crop para escolher o layout.
        """
        h, w = crop.shape[:2]
        out = crop.copy()
        aspect = w / max(h, 1)

        # Referência de cor = faixa central livre de artefatos
        cx0, cx1 = int(w * 0.20), int(w * 0.88)
        cy0, cy1 = int(h * 0.10), int(h * 0.80)
        ref = out[cy0:cy1, cx0:cx1]
        if ref.size == 0:
            ref = out
        fill = ref.reshape(-1, 3).mean(axis=0).astype(np.uint8) if out.ndim == 3 else int(ref.mean())

        if aspect > 2.0:  # carro
            qw, qh = int(w * 0.14), int(h * 0.38)
            mw, mh = int(w * 0.12), int(h * 0.30)
        else:             # moto
            qw, qh = int(w * 0.20), int(h * 0.28)
            mw, mh = int(w * 0.18), int(h * 0.30)

        out[:max(1, qh), :max(1, qw)] = fill
        out[h - max(1, mh):, :max(1, mw)] = fill

        return out

    def _focar_caracteres(self, crop) -> np.ndarray:
        """Crop automático nos caracteres grandes por projeção de pixels.

        Binariza a imagem, inverte (chars escuros → foreground), soma pixels
        por linha e por coluna e encontra os limites onde há conteúdo real.
        Elimina margens em branco, bordas e artefatos residuais pequenos.

        Funciona em imagem colorida ou grayscale/binarizada.
        """
        h, w = crop.shape[:2]

        # Grayscale para análise de projeção
        cinza = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop

        # Binariza e inverte: chars escuros (foreground) → 255
        _, bin_ = cv2.threshold(cinza, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

        proj_v = bin_.sum(axis=0).astype(np.float32)   # soma por coluna
        proj_h = bin_.sum(axis=1).astype(np.float32)   # soma por linha

        if proj_v.max() == 0 or proj_h.max() == 0:
            return crop

        # Linhas/colunas com conteúdo acima de 5% do pico
        th_v = proj_v.max() * 0.05
        th_h = proj_h.max() * 0.05
        cols = np.where(proj_v > th_v)[0]
        rows = np.where(proj_h > th_h)[0]

        if len(cols) < 4 or len(rows) < 4:
            return crop

        x1, x2 = int(cols[0]), int(cols[-1]) + 1
        y1, y2 = int(rows[0]), int(rows[-1]) + 1

        # Margem de 4% para não cortar ascendentes/descendentes
        pad = max(3, int(min(h, w) * 0.04))
        x1 = max(0, x1 - pad)
        x2 = min(w, x2 + pad)
        y1 = max(0, y1 - pad)
        y2 = min(h, y2 + pad)

        return crop[y1:y2, x1:x2]

    def _preprocessar(self, crop) -> np.ndarray:
        """Tesseract: remove artefatos → foca chars → grayscale → escala → binarização."""
        if crop.size == 0:
            return crop
        if crop.ndim == 3:
            crop = self._corrigir_perspectiva(crop)
            crop, tinha_header, e_mercosul = self._remover_header(crop)
            if tinha_header and e_mercosul:
                crop = self._remover_ruidos_mercosul(crop)
            crop = self._focar_caracteres(crop)
            cinza = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        else:
            cinza = crop.copy()

        # Escala mínima 120px — melhor acurácia Tesseract em placas largas
        h = cinza.shape[0]
        if h < 120:
            fator = 120 / max(h, 1)
            cinza = cv2.resize(cinza, None, fx=fator, fy=fator, interpolation=cv2.INTER_CUBIC)

        cinza = cv2.GaussianBlur(cinza, (3, 3), 0)

        # Otsu como primário; adaptativo como fallback se Otsu binarizar mal
        _, otsu = cv2.threshold(cinza, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        pct_branco = np.count_nonzero(otsu) / otsu.size
        if 0.45 < pct_branco < 0.95:
            return otsu

        return cv2.adaptiveThreshold(
            cinza, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10
        )

    def _preprocessar_dl(self, crop) -> np.ndarray:
        """Deep learning: remove artefatos → foca chars → escala mínima."""
        if crop.size == 0:
            return crop
        if crop.ndim == 3:
            # Smooths H.264/RTSP block artifacts before any other processing
            _, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 95])
            crop = cv2.imdecode(buf, cv2.IMREAD_COLOR)
            crop = self._corrigir_perspectiva(crop)
            crop, tinha_header, e_mercosul = self._remover_header(crop)
            if tinha_header and e_mercosul:
                crop = self._remover_ruidos_mercosul(crop)
            crop = self._focar_caracteres(crop)
        h = crop.shape[0]
        if h < 80:
            fator = 80 / max(h, 1)
            crop = cv2.resize(crop, None, fx=fator, fy=fator, interpolation=cv2.INTER_CUBIC)
        return crop

    # -- Leitura ---------------------------------------------------------------

    def ler(self, crop) -> tuple[str, float]:
        if crop is None or crop.size == 0:
            return "", 0.0

        engine = self.engine

        if engine == "tesseract":
            img = self._preprocessar(crop)
            estado.registrar_crop_ocr(img)
            return self._ler_tesseract(img)

        # fast-plate-ocr: só perspectiva, sem remoção de header.
        # O modelo ViT foi treinado em placas completas (com a faixa colorida).
        # Remover o header muda a distribuição de entrada e piora a leitura.
        if engine == "fast_plate_ocr" and self._fast_plate is not None:
            img_fp = self._corrigir_perspectiva(crop) if crop.ndim == 3 else crop
            estado.registrar_crop_ocr(img_fp)
            return self._ler_fast_plate_ocr(img_fp)

        # Outros engines DL: preprocessamento completo (remove header + artefatos)
        img = self._preprocessar_dl(crop)
        estado.registrar_crop_ocr(img)

        if engine == "easyocr" and self._easyocr_reader is not None:
            return self._ler_easyocr(img)
        if engine == "paddleocr" and self._paddle is not None:
            return self._ler_paddleocr(img)
        if engine == "doctr" and self._doctr is not None:
            return self._ler_doctr(img)

        # Fallback se engine não inicializou corretamente
        img_t = self._preprocessar(crop)
        estado.registrar_crop_ocr(img_t)
        return self._ler_tesseract(img_t)

    def _ler_tesseract(self, img) -> tuple[str, float]:
        try:
            import pytesseract
        except ImportError:
            return "", 0.0
        psms = list(dict.fromkeys([self.psm, 6, 11]))
        for psm in psms:
            texto, conf = self._tentar_psm(pytesseract, img, psm)
            if texto:
                return texto, conf
        return "", 0.0

    def _tentar_psm(self, pytesseract, img, psm: int) -> tuple[str, float]:
        config_str = f"--oem 3 --psm {psm} -c tessedit_char_whitelist={CHARS_VALIDOS}"
        try:
            data = pytesseract.image_to_data(img, config=config_str, output_type=pytesseract.Output.DICT)
            textos, confs = [], []
            for txt, conf in zip(data["text"], data["conf"]):
                txt = re.sub(r"[^A-Z0-9]", "", txt.upper())
                try:
                    conf_v = float(conf)
                except (TypeError, ValueError):
                    conf_v = -1
                if txt and conf_v > 0:
                    textos.append(txt)
                    confs.append(conf_v)
            texto = "".join(textos)
            confianca = (sum(confs) / len(confs) / 100.0) if confs else 0.0
            log.debug("Tesseract psm=%d → %r (conf=%.2f)", psm, texto, confianca)
            return texto, confianca
        except Exception as e:
            log.error("Erro Tesseract psm=%d: %s", psm, e)
            return "", 0.0

    def _ler_easyocr(self, img) -> tuple[str, float]:
        try:
            resultados = self._easyocr_reader.readtext(img, allowlist=CHARS_VALIDOS, detail=1)
        except Exception as e:
            log.error("Erro EasyOCR: %s", e)
            return "", 0.0
        if not resultados:
            log.info("EasyOCR: nenhum texto detectado (img %dx%d)", img.shape[1], img.shape[0])
            return "", 0.0
        for r in resultados:
            log.info("EasyOCR box: %r conf=%.2f", r[1].upper(), float(r[2]))
        textos = [r[1].upper() for r in resultados]
        confs = [float(r[2]) for r in resultados]
        texto = re.sub(r"[^A-Z0-9]", "", "".join(textos))
        conf_media = sum(confs) / len(confs)
        log.info("EasyOCR combinado: %r conf=%.2f", texto, conf_media)
        return texto, conf_media

    def _ler_paddleocr(self, img) -> tuple[str, float]:
        try:
            result = self._paddle.ocr(img, cls=False)
            if not result or not result[0]:
                return "", 0.0
            textos = [r[1][0] for r in result[0]]
            confs = [float(r[1][1]) for r in result[0]]
            texto = re.sub(r"[^A-Z0-9]", "", "".join(textos).upper())
            return texto, sum(confs) / len(confs)
        except Exception as e:
            log.error("Erro PaddleOCR: %s", e)
            return "", 0.0

    def _ler_doctr(self, img) -> tuple[str, float]:
        try:
            img_rgb = (
                cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                if img.ndim == 3
                else cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
            )
            from doctr.io import DocumentFile
            doc = DocumentFile.from_images([img_rgb])
            result = self._doctr(doc)
            textos, confs = [], []
            for page in result.pages:
                for block in page.blocks:
                    for line in block.lines:
                        for word in line.words:
                            textos.append(word.value)
                            confs.append(word.confidence)
            texto = re.sub(r"[^A-Z0-9]", "", "".join(textos).upper())
            conf = sum(confs) / len(confs) if confs else 0.0
            return texto, conf
        except Exception as e:
            log.error("Erro docTR: %s", e)
            return "", 0.0

    def _ler_fast_plate_ocr(self, img) -> tuple[str, float]:
        try:
            # Modelo exige grayscale (H, W)
            cinza = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
            result = self._fast_plate.run(cinza, return_confidence=True)
            if not result:
                log.info("fast-plate-ocr: sem resultado (img %dx%d)", img.shape[1], img.shape[0])
                return "", 0.0
            pred = result[0]
            texto = re.sub(r"[^A-Z0-9]", "", pred.plate.upper())
            conf = float(pred.char_probs.mean()) if pred.char_probs is not None else 0.8
            if pred.char_probs is not None:
                por_char = " ".join(f"{c:.2f}" for c in pred.char_probs)
                log.info("fast-plate-ocr: %r conf=%.2f [%s]", texto, conf, por_char)
            else:
                log.info("fast-plate-ocr: %r conf=%.2f", texto, conf)
            return texto, conf
        except Exception as e:
            log.error("Erro fast-plate-ocr: %s", e)
            return "", 0.0


def _realcar_para_ocr(crop, alvo_h: int = 224, limiar_blur: float = 3500.0):
    """Amplia + afia o crop APENAS quando ele está borrado (placa distante/baixa-res).

    O gatilho é a NITIDEZ do crop (variância do Laplaciano), não o tamanho: crop nítido
    (lapvar alto) passa intacto — sharpen nele criaria artefatos e PIORARIA o OCR. Crop
    borrado (lapvar baixo) é ampliado por interpolação cúbica e afiado, recuperando as
    bordas dos caracteres.

    Calibrado em placas reais (UFPR-ALPR) vs sintéticas nítidas:
      - nítidas: lapvar ≥ 4554  → não mexe
      - borradas reais: lapvar mediano ~1400 (p90 ~3200) → realça
    Efeito no OCR de placas borradas reais: 48% → 60% (+12pp), sem regressão nas nítidas.
    """
    if crop is None or crop.size == 0 or crop.ndim != 3:
        return crop
    cinza = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    if cv2.Laplacian(cinza, cv2.CV_64F).var() >= limiar_blur:
        return crop  # já nítido — não mexe
    h = crop.shape[0]
    if h < alvo_h:
        f = alvo_h / max(h, 1)
        crop = cv2.resize(crop, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
    # Unsharp mask SUAVE (amount 0.6) — recupera nitidez sem os artefatos de um kernel
    # forte, que criavam confusões de caractere (O→D) em placas já limítrofes.
    borrado = cv2.GaussianBlur(crop, (0, 0), sigmaX=1.0)
    return cv2.addWeighted(crop, 1.6, borrado, -0.6, 0)


class AutoOCR:
    """Seleciona o engine automaticamente pelo formato e tipo da placa:

    - Mercosul carro  (header + aspect > 2.0) → fast_plate_ocr
      (ViT treinado em linha única — ótimo para carro)
    - Mercosul moto   (header + aspect ≤ 2.0) → easyocr
      (2 linhas de texto — fast_plate_ocr confunde layout de moto)
    - Antigo          (sem header)            → easyocr

    Se o engine preferido não produzir leitura válida, usa o outro como fallback.
    Interface compatível com OCR e MultiOCR (.carregar(), .ler(), .ler_detalhado()).
    """

    def __init__(self, tesseract_psm: int = 7):
        self._fast = OCR(engine="fast_plate_ocr", tesseract_psm=tesseract_psm)
        self._easy = OCR(engine="easyocr", tesseract_psm=tesseract_psm)
        self.engine = "auto"
        self._ultimo_detalhe: dict = {}

    def carregar(self) -> None:
        self._fast.carregar()
        self._easy.carregar()

    def ler(self, crop) -> tuple[str, float]:
        det = self.ler_detalhado(crop)
        self._ultimo_detalhe = det
        return det["placa"] or "", det["confianca"]

    def ler_detalhado(self, crop) -> dict:
        from validador import validar

        # Realce (upscale + sharpen) — recupera placas pequenas/borradas antes do OCR.
        crop = _realcar_para_ocr(crop)

        tinha_header = False
        e_mercosul_header = False
        if crop is not None and crop.ndim == 3 and crop.size > 0:
            _, tinha_header, e_mercosul_header = self._fast._remover_header(crop)

        # Moto: aspect ≤ 2 (200×140 vs 400×130) — 2 linhas de texto, easyocr é superior
        aspect = (crop.shape[1] / max(crop.shape[0], 1)) if crop is not None else 3.0
        e_moto = tinha_header and aspect <= 2.0

        # fast_plate_ocr como principal apenas para Mercosul carro
        # (tinha header colorido + não é moto + header era Mercosul azul/verde)
        if tinha_header and not e_moto and e_mercosul_header:
            principal, fallback = self._fast, self._easy   # Mercosul carro
        else:
            principal, fallback = self._easy, self._fast   # Moto Mercosul ou antigo

        # Quando header Mercosul confirmado, passa hint para validar() priorizar
        # correção Mercosul antes de aceitar antigo (ex: FBI0123 → FBI0I23 moto)
        formato_hint = "mercosul" if tinha_header and e_mercosul_header else ""

        tipo_placa = ("moto-mercosul" if e_moto else ("mercosul-carro" if e_mercosul_header else "antigo"))
        h, w = (crop.shape[:2] if crop is not None else (0, 0))
        log.info(
            "AutoOCR: crop=%dx%d aspect=%.2f tipo=%s header=%s mercosul=%s principal=%s",
            w, h, aspect, tipo_placa, tinha_header, e_mercosul_header, principal.engine,
        )

        texto, conf = principal.ler(crop)
        resultado = validar(texto, formato_hint)
        log.info(
            "AutoOCR %s: bruto=%r → validado=%r conf=%.2f",
            principal.engine, texto, resultado[0] if resultado else None, conf,
        )
        detalhes = [{
            "engine": principal.engine,
            "placa": resultado[0] if resultado else None,
            "padrao": resultado[1] if resultado else None,
            "confianca": round(conf, 3),
        }]

        # Aceita sem tentar fallback apenas se: não é moto E confiança alta.
        # Moto tem 2 linhas de texto e erra mais — sempre compara os dois engines.
        # Confiança baixa (< 50%) também força comparação para evitar leituras erradas.
        if resultado and not e_moto and conf >= 0.50:
            log.info("AutoOCR: aceito %r conf=%.2f (sem fallback)", resultado[0], conf)
            return {
                "placa": resultado[0], "padrao": resultado[1],
                "confianca": round(conf, 3),
                "votos": 1, "total_engines": 1, "detalhes": detalhes,
            }

        motivo_fallback = "moto" if e_moto else ("conf_baixa=%.2f" % conf if resultado else "sem_resultado")
        log.info("AutoOCR: rodando fallback=%s motivo=%s", fallback.engine, motivo_fallback)

        # Executa fallback: sempre para moto, ou quando principal falhou/conf baixa
        texto2, conf2 = fallback.ler(crop)
        resultado2 = validar(texto2, formato_hint)
        log.info(
            "AutoOCR %s: bruto=%r → validado=%r conf=%.2f",
            fallback.engine, texto2, resultado2[0] if resultado2 else None, conf2,
        )
        detalhes.append({
            "engine": fallback.engine,
            "placa": resultado2[0] if resultado2 else None,
            "padrao": resultado2[1] if resultado2 else None,
            "confianca": round(conf2, 3),
        })

        # Ambos validam: escolhe o vencedor.
        if resultado and resultado2:
            if e_moto:
                # Moto (2 linhas): o principal é a EasyOCR, confiável nesse layout.
                # O fast_plate_ocr é treinado em 1 linha e às vezes valida uma leitura
                # ERRADA com confiança alta — NÃO deve sobrepor a EasyOCR aqui.
                melhor, melhor_conf, vencedor = resultado, conf, principal.engine
            else:
                melhor = resultado2 if conf2 > conf else resultado
                melhor_conf = conf2 if conf2 > conf else conf
                vencedor = fallback.engine if conf2 > conf else principal.engine
            log.info(
                "AutoOCR: ambos validaram — %s(%r %.2f) vs %s(%r %.2f) → vencedor=%s(%r)",
                principal.engine, resultado[0], conf,
                fallback.engine, resultado2[0], conf2,
                vencedor, melhor[0],
            )
            return {
                "placa": melhor[0], "padrao": melhor[1],
                "confianca": round(melhor_conf, 3),
                "votos": 1, "total_engines": 2, "detalhes": detalhes,
            }

        if resultado:
            log.info("AutoOCR: somente principal validou → %r conf=%.2f", resultado[0], conf)
            return {
                "placa": resultado[0], "padrao": resultado[1],
                "confianca": round(conf, 3),
                "votos": 1, "total_engines": 2, "detalhes": detalhes,
            }

        if resultado2:
            log.info("AutoOCR: somente fallback validou → %r conf=%.2f", resultado2[0], conf2)
            return {
                "placa": resultado2[0], "padrao": resultado2[1],
                "confianca": round(conf2, 3),
                "votos": 1, "total_engines": 2, "detalhes": detalhes,
            }

        log.info("AutoOCR: nenhum engine validou (principal=%r fallback=%r)", texto, texto2)
        return {
            "placa": None, "padrao": None, "confianca": 0.0,
            "votos": 0, "total_engines": 2, "detalhes": detalhes,
        }


class MultiOCR:
    """Executa múltiplos engines e elege o resultado por votação majoritária.

    Interface compatível com OCR (mesmos .carregar() e .ler()).
    Adiciona .ler_detalhado() que devolve votos e resultado por engine.
    """

    def __init__(self, engines: list[str], tesseract_psm: int = 7):
        # Remove duplicatas preservando ordem; garante ao menos um engine
        vistos: set[str] = set()
        unicos = []
        for e in engines:
            if e and e not in vistos:
                vistos.add(e)
                unicos.append(e)
        if not unicos:
            unicos = ["tesseract"]
        self._ocrs = [OCR(engine=e, tesseract_psm=tesseract_psm) for e in unicos]
        self.engine = ",".join(unicos)  # compatibilidade com estado.ocr_engine_ativo
        self._ultimo_detalhe: dict = {}

    def carregar(self) -> None:
        for ocr in self._ocrs:
            ocr.carregar()

    def ler(self, crop) -> tuple[str, float]:
        det = self.ler_detalhado(crop)
        self._ultimo_detalhe = det
        return det["placa"] or "", det["confianca"]

    def ler_detalhado(self, crop) -> dict:
        """Roda todos os engines e retorna votação + detalhes individuais."""
        from collections import Counter
        from validador import validar

        detalhes = []
        for ocr in self._ocrs:
            texto_bruto, conf = ocr.ler(crop)
            resultado = validar(texto_bruto)
            detalhes.append({
                "engine": ocr.engine,
                "placa": resultado[0] if resultado else None,
                "padrao": resultado[1] if resultado else None,
                "confianca": round(conf, 3),
            })

        validos = [(d["placa"], d["confianca"]) for d in detalhes if d["placa"]]
        total = len(self._ocrs)

        if not validos:
            return {
                "placa": None, "padrao": None, "confianca": 0.0,
                "votos": 0, "total_engines": total, "detalhes": detalhes,
            }

        votos = Counter(p for p, _ in validos)
        placa, n_votos = votos.most_common(1)[0]
        confs = [c for p, c in validos if p == placa]
        padrao = next(d["padrao"] for d in detalhes if d["placa"] == placa)

        return {
            "placa": placa,
            "padrao": padrao,
            "confianca": round(sum(confs) / len(confs), 3),
            "votos": n_votos,
            "total_engines": total,
            "detalhes": detalhes,
        }
