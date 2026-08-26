"""Engines de OCR e pré-processamento de imagem por engine.

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

from app.core import estado

log = logging.getLogger(__name__)

CHARS_VALIDOS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

# Membros default do ensemble do fast-plate-ocr. TRÊS modelos, e não um, porque a
# diversidade entre eles é o que a fusão por caractere consome — medido nas 30 fotos
# rotuladas (26 carro + 4 moto), fundindo com `visao.consenso.consenso_caractere`:
#
#     1 modelo  (o que havia) .... carro 13/26 (50%)   moto 0/4
#     2 modelos ................. carro 15/26 (58%)   moto 2/4
#     3 modelos ................. carro 17/26 (65%)   moto 3/4
#
# O ganho é de MODELO, não de pré-processamento: rodar o mesmo modelo em 3 variantes de
# imagem (perspectiva, cortar-as-2-linhas-e-colar) mede 11/26 — PIOR que o modelo sozinho.
# E o voto tem de ser PLANO, um por modelo: consolidar a família num voto antes de fundir
# com os outros engines derruba para 13/26, porque a concordância entre os três É o sinal.
#
# `global-plates-mobile-vit-v2` é grayscale 70x140 e os `cct-*` são RGB 64x128 — a conversão
# sai de `config['image_color_mode']` de cada modelo, nunca do nome: passar grayscale a um
# modelo RGB estoura `InvalidArgument` no onnxruntime.
FAST_MODELOS_DEFAULT = (
    "global-plates-mobile-vit-v2-model",
    "cct-s-v2-global-model",
    "cct-xs-v2-global-model",
)


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
    def __init__(self, engine: str = "tesseract", tesseract_psm: int = 7,
                 deskew_ativo: bool = True, deskew_angulo_max: float = 30.0,
                 fast_modelos: tuple[str, ...] | None = None):
        self.engine = engine
        self.psm = tesseract_psm
        self._deskew_ativo = deskew_ativo
        self._deskew_angulo_max = deskew_angulo_max
        self._easyocr_reader = None
        self._paddle = None
        self._doctr = None
        self._fast_plate = None
        # Membros do ensemble: [(nome, recognizer, cor)] — ver `FAST_MODELOS_DEFAULT`.
        # `_fast_plate` continua apontando para o PRIMEIRO membro porque é o atributo que
        # `ler()` e os dublês de teste checam para saber se o engine subiu.
        self._fast_modelos = tuple(fast_modelos) if fast_modelos else FAST_MODELOS_DEFAULT
        self._fast_membros: list[tuple[str, object, str]] = []
        # Marca se a última passada de `_preprocessar_dl` chegou a apagar QR/"BR" — é o que
        # deixa `ler()` saber que vale repetir sem essa limpeza quando o OCR volta vazio.
        self._ultimo_limpou_mercosul = False

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
        from app.visao.hardware import torch_cuda_disponivel
        usar_gpu = torch_cuda_disponivel()
        self._easyocr_reader = easyocr.Reader(["en"], gpu=usar_gpu, verbose=False)
        log.info("EasyOCR carregado (gpu=%s)", usar_gpu)
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
        # GPU: paddle não tem parâmetro de device no construtor do PaddleOCR — é um
        # contexto GLOBAL (paddle.set_device). Só funciona se o pacote instalado for
        # paddlepaddle-gpu (o paddlepaddle comum não tem CUDA compilado — is_compiled_
        # with_cuda() retorna False e cai pra CPU automaticamente). NÃO verificado com
        # GPU real (ambiente de desenvolvimento é CPU-only) — validar em produção.
        try:
            import paddle
            if paddle.device.is_compiled_with_cuda():
                paddle.set_device("gpu")
                log.info("PaddleOCR: GPU CUDA disponível (paddlepaddle-gpu) — usando GPU")
        except Exception as e:
            log.warning("PaddleOCR: falha ao configurar GPU (%s) — usando CPU", e)
        # API 3.x (PP-OCRv5/v6). enable_mkldnn=False evita um bug do oneDNN no executor
        # PIR do paddlepaddle 3.x em CPU (irrelevante em GPU, oneDNN é CPU-only).
        # Desliga pré/pós de documento (é crop de placa).
        self._paddle = PaddleOCR(
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            lang="en",
            enable_mkldnn=False,
        )
        log.info("PaddleOCR carregado (PP-OCR 3.x, mkldnn off)")

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
        self._fast_membros = []
        for nome in self._fast_modelos:
            # Um membro que não baixa/carrega NÃO derruba o ensemble: os outros seguem, e a
            # fusão só perde um voto. Derrubar tudo por causa de um download que falhou
            # deixaria o posto sem OCR nenhum.
            try:
                rec = LicensePlateRecognizer(nome)
            except Exception as e:
                log.error("fast-plate-ocr: membro %s não carregou (%s) — segue sem ele", nome, e)
                continue
            self._fast_membros.append((nome, rec, _cor_do_modelo(rec)))
        if not self._fast_membros:
            log.error("fast-plate-ocr: nenhum membro carregou — caindo para tesseract")
            self.engine = "tesseract"
            self._carregar_tesseract()
            return
        self._fast_plate = self._fast_membros[0][1]
        log.info("fast-plate-ocr carregado: %d modelo(s) [%s]", len(self._fast_membros),
                 ", ".join(n for n, _, _ in self._fast_membros))
        # Warm-up: ONNX Runtime otimiza o grafo na primeira execução — fazer no startup, e
        # em CADA membro: aquecer só o primeiro deixaria a otimização dos outros para o
        # primeiro "Ler Placa" de verdade.
        for nome, rec, cor in self._fast_membros:
            try:
                forma = (50, 200, 3) if cor == "rgb" else (50, 200)
                rec.run(np.zeros(forma, dtype=np.uint8))
            except Exception as e:
                log.debug("fast-plate-ocr: warm-up de %s falhou (%s)", nome, e)
        log.info("fast-plate-ocr aquecido")

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

    def _deskew(self, crop_bgr) -> np.ndarray:
        """Corrige inclinação rotacional (skew 2D) da placa.

        Detecta o ângulo de inclinação pelo retângulo de mínima área que
        circunscreve os contornos de texto (chars escuros sobre fundo claro).
        Aplica rotação via warpAffine para horizontalizar.

        Faixa ativa: 0.5° ≤ |ângulo| ≤ deskew_angulo_max (padrão 30°).
        Fora dessa faixa, retorna o crop original sem modificação.
        """
        if not self._deskew_ativo:
            return crop_bgr
        if crop_bgr.ndim != 3 or crop_bgr.size == 0:
            return crop_bgr
        h, w = crop_bgr.shape[:2]
        if h < 20 or w < 20:
            return crop_bgr

        # Binariza: chars escuros → foreground branco
        cinza = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
        borrado = cv2.GaussianBlur(cinza, (5, 5), 0)
        _, bin_ = cv2.threshold(borrado, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

        # Encontra contornos e une os pontos significativos
        contornos, _ = cv2.findContours(bin_, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contornos:
            return crop_bgr

        # Filtra contornos pequenos (ruído) — mantém apenas caracteres
        area_min = h * w * 0.002
        pontos = [cnt for cnt in contornos if cv2.contourArea(cnt) >= area_min]
        if not pontos:
            return crop_bgr

        todos = np.concatenate(pontos)
        rect = cv2.minAreaRect(todos)
        angulo = rect[-1]

        # Normaliza ângulo para [-45, 45]
        # OpenCV 4.5.1+: minAreaRect retorna [0, 90); versões anteriores [-90, 0)
        if angulo > 45:
            angulo -= 90
        elif angulo < -45:
            angulo += 90

        # Fora da faixa ativa: sem correção
        if abs(angulo) < 0.5 or abs(angulo) > self._deskew_angulo_max:
            return crop_bgr

        # Rotação — BORDER_REPLICATE evita bordas pretas que confundem OCR
        cx, cy = w / 2, h / 2
        M = cv2.getRotationMatrix2D((cx, cy), angulo, 1.0)
        rotacionado = cv2.warpAffine(
            crop_bgr, M, (w, h),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
        )
        log.debug("Deskew: ângulo=%.1f° corrigido (%dx%d)", angulo, w, h)
        return rotacionado

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
                mask = cv2.inRange(hsv_h, np.array([80, 50, 50]), np.array([140, 255, 255]))
                azul_ratio = cv2.countNonZero(mask) / mask.size
                e_mercosul = azul_ratio > 0.15
                sat_media = float(hsv_h[:, :, 1].mean())
            else:
                sat_media = 0.0
                e_mercosul = False
            log.debug(
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
            log.debug(
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
            crop = self._deskew(crop)
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

    def _preprocessar_dl(self, crop, limpar_mercosul: bool = True) -> np.ndarray:
        """Deep learning: remove artefatos → foca chars → escala mínima.

        `limpar_mercosul=False` pula só a etapa de apagar QR/"BR" — usado na segunda
        tentativa de `ler()`, ver o comentário lá.
        """
        if crop.size == 0:
            return crop
        if crop.ndim == 3:
            # Smooths H.264/RTSP block artifacts before any other processing
            _, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 95])
            crop = cv2.imdecode(buf, cv2.IMREAD_COLOR)
            crop = self._deskew(crop)
            crop = self._corrigir_perspectiva(crop)
            crop, tinha_header, e_mercosul = self._remover_header(crop)
            self._ultimo_limpou_mercosul = bool(tinha_header and e_mercosul and limpar_mercosul)
            if self._ultimo_limpou_mercosul:
                crop = self._remover_ruidos_mercosul(crop)
            crop = self._focar_caracteres(crop)
        h = crop.shape[0]
        if h < 80:
            fator = 80 / max(h, 1)
            crop = cv2.resize(crop, None, fx=fator, fy=fator, interpolation=cv2.INTER_CUBIC)
        return crop

    # -- Leitura ---------------------------------------------------------------

    def ler_varias(self, crop) -> list[tuple[str, float]]:
        """TODAS as leituras que este engine tem a oferecer para o recorte.

        Existe para alimentar a fusão por caractere em vez de a arbitragem: o
        `fast_plate_ocr` devolve uma leitura por membro do ensemble, e é a discordância
        entre elas que `visao.consenso.consenso_caractere` converte em placa. Os outros
        engines devolvem uma leitura só — a lista de um elemento mantém o chamador com um
        caminho único, sem `isinstance` nem `hasattr` espalhados.

        O pré-processamento é o MESMO de `ler()` de propósito. Medido nas 30 fotos
        rotuladas com o ensemble de 3 modelos: `deskew + perspectiva` (o que o sistema já
        fazia) dá moto 3/4, contra 2/4 no recorte cru, com carro igual em 17/26. Trocar o
        pré-processamento junto com o ensemble mexeria em duas variáveis de uma vez.
        """
        if crop is None or crop.size == 0:
            return []
        if self._fast_pronto():
            # FILTRA sem reconstruir: `[(t, c) for t, c in ...]` monta tuplas novas e
            # DESCARTA o `por_char` de cada `LeituraOCR` — o modo de falha que o docstring
            # daquela classe descreve. Passar o objeto adiante é o que preserva o atributo.
            return [l for l in self._ler_fast_plate_varias(self._preparar_fast(crop)) if l[0]]
        texto, conf = self.ler(crop)
        # Sem `por_char`: os outros engines não expõem confiança por caractere. A fusão cai
        # para o peso escalar nessas leituras, que é o comportamento de antes.
        return [LeituraOCR(texto, conf)] if texto else []

    def _fast_pronto(self) -> bool:
        """Este OCR e o fast-plate-ocr E tem ao menos um membro utilizavel?"""
        return (self.engine == "fast_plate_ocr"
                and bool(self._fast_membros or self._fast_plate is not None))

    def _preparar_fast(self, crop):
        """Pre-processamento do fast-plate-ocr: so deskew + perspectiva, SEM remover header.

        O modelo foi treinado em placas completas, com a faixa colorida. Remover o header
        muda a distribuicao de entrada e piora a leitura.

        Funcao propria porque `ler` e `ler_varias` precisam da MESMA preparacao, e o
        pre-processamento do fast e uma decisao medida (com deskew+perspectiva: moto 3/4;
        no recorte cru: 2/4, com carro igual). Escrita duas vezes, ela diverge na primeira
        vez que alguem ajustar so um dos lados - o mesmo motivo que fez
        `app/visao/consenso.py` existir.
        """
        img = crop
        if crop.ndim == 3:
            img = self._corrigir_perspectiva(self._deskew(crop))
        estado.registrar_crop_ocr(img)
        return img

    def ler(self, crop) -> tuple[str, float]:
        if crop is None or crop.size == 0:
            return "", 0.0

        engine = self.engine

        if engine == "tesseract":
            img = self._preprocessar(crop)
            estado.registrar_crop_ocr(img)
            return self._ler_tesseract(img)

        # fast-plate-ocr: preparacao propria (ver `_preparar_fast`), e a MELHOR leitura do
        # ensemble. Quem quer o ensemble inteiro chama `ler_varias`.
        if self._fast_pronto():
            return self._ler_fast_plate_ocr(self._preparar_fast(crop))

        # Outros engines DL: preprocessamento completo (remove header + artefatos)
        self._ultimo_limpou_mercosul = False
        img = self._preprocessar_dl(crop)
        estado.registrar_crop_ocr(img)

        resultado = None
        if engine == "easyocr" and self._easyocr_reader is not None:
            resultado = self._ler_easyocr(img)
        elif engine == "paddleocr" and self._paddle is not None:
            resultado = self._ler_paddleocr(img)
        elif engine == "doctr" and self._doctr is not None:
            resultado = self._ler_doctr(img)

        if resultado is not None:
            # `_remover_ruidos_mercosul` PINTA POR CIMA dos cantos esquerdos (20%x28% em
            # cima, 18%x30% embaixo) para apagar QR do CRLV-e e o marcador "BR". Quando o
            # crop não é Mercosul de verdade, isso cobre o primeiro caractere de cada
            # linha e o OCR não acha texto nenhum. Acontece de fato: numa placa ANTIGA de
            # moto real do posto, `_remover_header` devolveu e_mercosul=True (falso
            # positivo pela faixa metálica), a limpeza apagou o 'Y' e o '5' e o Paddle
            # passou de 'NOI'+'5947' para nada.
            #
            # Em vez de tentar acertar a classificação do header (mexer nela arrisca as
            # Mercosul de verdade, que dependem da limpeza), tenta de novo SEM a limpeza
            # quando ela rodou e o resultado veio vazio. Custa uma passada extra só no
            # caso que já tinha falhado.
            if not resultado[0] and self._ultimo_limpou_mercosul:
                img2 = self._preprocessar_dl(crop, limpar_mercosul=False)
                estado.registrar_crop_ocr(img2)
                if engine == "easyocr":
                    retry = self._ler_easyocr(img2)
                elif engine == "paddleocr":
                    retry = self._ler_paddleocr(img2)
                else:
                    retry = self._ler_doctr(img2)
                if retry[0]:
                    log.debug("OCR recuperado sem a limpeza Mercosul: %r", retry[0])
                    return retry
            return resultado

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
            log.debug("EasyOCR: nenhum texto detectado (img %dx%d)", img.shape[1], img.shape[0])
            return "", 0.0
        for r in resultados:
            log.debug("EasyOCR box: %r conf=%.2f", r[1].upper(), float(r[2]))
        textos = [r[1].upper() for r in resultados]
        confs = [float(r[2]) for r in resultados]
        texto = re.sub(r"[^A-Z0-9]", "", "".join(textos))
        conf_media = sum(confs) / len(confs)
        log.debug("EasyOCR combinado: %r conf=%.2f", texto, conf_media)
        return texto, conf_media

    # Caixa com área abaixo desta fração da maior é texto acessório (cidade/UF, "BRASIL",
    # "DETRAN"), não uma linha da placa. As duas linhas de uma placa de moto têm áreas
    # comparáveis (0.60-0.95 da maior, medido); os acessórios ficam bem abaixo (0.02-0.22).
    FRACAO_LINHA = 0.35

    def _ler_paddleocr(self, img) -> tuple[str, float]:
        """PaddleOCR 3.x → texto da placa a partir das caixas detectadas.

        Carro (1 linha): fica com a MAIOR caixa — a placa é o maior texto do crop e
        'BRASIL'/cidade/estado são menores, então a maior isola a placa do resto.

        Moto (2 linhas): a mesma regra era DESTRUTIVA. Numa placa de moto as duas linhas
        (letras em cima, dígitos embaixo) são caixas separadas e de tamanho parecido, então
        "a maior" jogava fora metade da placa — sempre. Medido nas 27 placas de moto de
        `testes/dataset.json`: 0/27, e em TODAS o retorno era uma das duas linhas sozinha
        ('YZA3456' saía '3456', 'NOP5Q67' saía 'NOP'). Não era resolução: são sintéticas e
        limpas. Aqui as caixas comparáveis à maior são unidas em ORDEM DE LEITURA
        (cima→baixo, esquerda→direita), que é a ordem dos caracteres na placa.

        A separação carro/moto NÃO é feita pela proporção do crop, apesar de ser o
        reflexo natural. Esta função recebe a imagem já pré-processada, e `_focar_caracteres`
        corta o cabeçalho: uma placa de moto de 200x140 (proporção 1.43) chega aqui como
        200x81 (proporção 2.47) e seria classificada como carro — foi exatamente o que
        deixou 11 das 27 ainda quebradas na primeira versão desta correção. Quem decide é
        a geometria das caixas: o filtro de área separa linha-de-placa de texto acessório,
        e juntar em ordem de leitura é o comportamento certo nos dois casos (num carro cuja
        placa o Paddle porventura parta em duas caixas, unir também é o certo).
        """
        try:
            res = self._paddle.predict(img)
        except Exception as e:
            log.error("Erro PaddleOCR: %s", e)
            return "", 0.0

        caixas: list[tuple[float, float, float, str, float]] = []   # (cy, cx, area, txt, conf)
        for it in res or []:
            try:
                texts = it.get("rec_texts", []) or []
                scores = it.get("rec_scores", []) or [0.0] * len(texts)
                boxes = it.get("rec_boxes")
                if boxes is None:
                    boxes = it.get("rec_polys")
                boxes = list(boxes) if boxes is not None else [None] * len(texts)
            except Exception:
                continue
            for t, s, b in zip(texts, scores, boxes):
                txt = re.sub(r"[^A-Z0-9]", "", str(t).upper())
                if not txt:
                    continue
                cy, cx = _centro_caixa(b)
                caixas.append((cy, cx, _area_caixa(b), txt, float(s)))

        if not caixas:
            return "", 0.0

        maior = max(caixas, key=lambda c: c[2])
        if len(caixas) == 1:
            return maior[3], maior[4]

        linhas = [c for c in caixas if c[2] >= maior[2] * self.FRACAO_LINHA]
        linhas.sort(key=lambda c: (c[0], c[1]))
        texto = "".join(c[3] for c in linhas)
        # Confiança da linha PIOR, não a média: a placa só vale inteira, e uma linha
        # incerta compromete o resultado todo. A média esconderia isso atrás de uma
        # linha lida com 1.00.
        conf = min(c[4] for c in linhas)
        if len(linhas) > 1:
            log.debug("PaddleOCR moto: %d linhas unidas → %r (conf=%.2f)", len(linhas), texto, conf)
        return texto, conf

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

    def _ler_um_membro(self, nome: str, rec, cor: str, img) -> "LeituraOCR":
        """Uma leitura de UM membro do ensemble. Nunca levanta — devolve ("", 0.0) na falha.

        Engolir a exceção aqui é o que mantém o ensemble útil: um membro que quebra num
        recorte específico custa um voto, não a leitura inteira.
        """
        try:
            if cor == "rgb":
                entrada = (cv2.cvtColor(img, cv2.COLOR_BGR2RGB) if img.ndim == 3
                           else cv2.cvtColor(img, cv2.COLOR_GRAY2RGB))
            else:
                entrada = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
            result = rec.run(entrada, return_confidence=True)
            if not result:
                log.debug("fast-plate-ocr[%s]: sem resultado (img %dx%d)",
                          nome, img.shape[1], img.shape[0])
                return LeituraOCR("", 0.0)
            pred = result[0]
            texto = re.sub(r"[^A-Z0-9]", "", pred.plate.upper())
            conf = float(pred.char_probs.mean()) if pred.char_probs is not None else 0.8
            if pred.char_probs is not None:
                por_char = " ".join(f"{c:.2f}" for c in pred.char_probs)
                log.debug("fast-plate-ocr[%s]: %r conf=%.2f [%s]", nome, texto, conf, por_char)
            else:
                log.debug("fast-plate-ocr[%s]: %r conf=%.2f", nome, texto, conf)
            return LeituraOCR(texto, conf, _alinhar_por_char(texto, pred.char_probs, nome))
        except Exception as e:
            log.error("Erro fast-plate-ocr[%s]: %s", nome, e)
            return LeituraOCR("", 0.0)

    def _ler_fast_plate_varias(self, img) -> list[tuple[str, float]]:
        """Uma leitura POR MEMBRO do ensemble — a entrada da fusão por caractere.

        Devolve as leituras cruas, inclusive as que discordam entre si: é exatamente a
        discordância que `consenso_caractere` transforma em placa. Escolher a de maior
        confiança aqui jogaria fora o mecanismo (ver `FAST_MODELOS_DEFAULT`).
        """
        if self._fast_membros:
            return [self._ler_um_membro(n, r, c, img) for n, r, c in self._fast_membros]
        if self._fast_plate is not None:
            # Dublê de teste que injeta `_fast_plate` à mão, sem passar por `carregar()`.
            return [self._ler_um_membro("fast_plate_ocr", self._fast_plate,
                                        _cor_do_modelo(self._fast_plate), img)]
        return []

    def _ler_fast_plate_ocr(self, img) -> tuple[str, float]:
        """A MELHOR leitura do ensemble, para quem só sabe consumir uma (`ler`).

        Mantém o contrato `(texto, conf)` de que o pipeline contínuo e os dublês dependem.
        Quem quer o ensemble inteiro usa `ler_varias`.
        """
        leituras = [(t, c) for t, c in self._ler_fast_plate_varias(img) if t]
        if not leituras:
            return "", 0.0
        return max(leituras, key=lambda tc: tc[1])


class LeituraOCR(tuple):
    """`(texto, confianca)` que carrega, à parte, a confiança POR CARACTERE.

    Subclasse de `tuple` de tamanho 2 DE PROPÓSITO, e não uma 3-tupla: todo consumidor
    desempacota dois posicionalmente (`for t, c in ocr.ler_varias(...)`, os dublês de teste,
    `_leituras_do_engine` em ocr/auto.py). Assim a confiança por caractere viaja POR LEITURA
    sem que nada disso mude. Mesmo padrão, e mesmo motivo, de `BBoxPlaca` em
    `app/visao/detector.py`.

    `por_char` é `None` em toda leitura que não tem o vetor — engine que não expõe (EasyOCR,
    PaddleOCR, tesseract), dublê de teste, ou leitura em que o alinhamento não fechou. Quem
    consome tem de tratar a ausência, nunca presumir que existe: ver
    `consenso.consenso_caractere`, que aceita peso escalar ou por posição.

    Reconstruir a tupla DESCARTA o atributo (`tuple(l)`, `(l[0], l[1])`). O modo de falha é
    degradar para peso escalar — que é o comportamento anterior —, nunca indexar errado.
    """

    def __new__(cls, texto: str, conf: float, por_char=None):
        obj = super().__new__(cls, (texto, float(conf)))
        obj.por_char = por_char
        return obj


def _alinhar_por_char(texto: str, char_probs, nome: str):
    """Confiança por caractere alinhada ao `texto` já limpo, ou `None` se não der.

    O modelo devolve `char_probs` com um valor por SLOT (9 no `global-plates-mobile-vit-v2`,
    10 nos `cct-*`), e o texto sai com 7 depois de tirar o padding. O padding é final, então
    os `len(texto)` primeiros valores alinham — verificado no recorte da RLX2A77, onde
    `BLX2677` vem com `[0.99, 0.30, 0.97, 0.99, 0.22, 0.99, 0.99]` e as duas posições de
    confiança baixa são exatamente as duas erradas.

    Devolve `None` em vez de adivinhar quando o vetor é menor que o texto: um alinhamento
    errado colocaria a confiança de um caractere sobre outro, o que é pior que não ter
    confiança por caractere nenhuma — o consumidor cai para o escalar e nada se perde além
    da precisão extra.
    """
    if char_probs is None or not texto:
        return None
    try:
        vals = [float(x) for x in char_probs]
    except (TypeError, ValueError):
        return None
    if len(vals) < len(texto):
        log.debug("fast-plate-ocr[%s]: char_probs (%d) menor que o texto (%d) — usando só a "
                  "média", nome, len(vals), len(texto))
        return None
    return vals[:len(texto)]


def _cor_do_modelo(rec) -> str:
    """'rgb' ou 'grayscale' — o que ESTE modelo do fast-plate-ocr exige na entrada.

    Sai de `config['image_color_mode']` e nunca do nome do modelo: o zoo mistura os dois
    formatos (o `global-plates-mobile-vit-v2` é grayscale 70x140, os `cct-*` são RGB
    64x128) e alimentar um modelo RGB com grayscale não degrada a leitura — estoura
    `InvalidArgument: Got invalid dimensions for input` no onnxruntime, derrubando a
    passada inteira de OCR.

    Default 'grayscale' quando a config não diz nada: é o que o membro histórico usa.
    """
    cfg = getattr(rec, "config", None)
    if cfg is None:
        return "grayscale"
    valor = cfg.get("image_color_mode") if isinstance(cfg, dict) else getattr(cfg, "image_color_mode", None)
    return "rgb" if str(valor).strip().lower() == "rgb" else "grayscale"


def _area_caixa(b) -> float:
    """Área da caixa de um texto do PaddleOCR (rec_boxes [x1,y1,x2,y2] ou rec_polys 4 pts)."""
    if b is None:
        return 0.0
    try:
        a = np.asarray(b, dtype=float).reshape(-1)
    except Exception:
        return 0.0
    if a.size == 4:
        return abs((a[2] - a[0]) * (a[3] - a[1]))
    if a.size >= 8 and a.size % 2 == 0:
        xs, ys = a[0::2], a[1::2]
        return float((xs.max() - xs.min()) * (ys.max() - ys.min()))
    return 0.0


def _centro_caixa(b) -> tuple[float, float]:
    """Centro (y, x) da caixa — ordem de leitura: de cima para baixo, da esquerda p/ direita."""
    if b is None:
        return (0.0, 0.0)
    try:
        a = np.asarray(b, dtype=float).reshape(-1)
    except Exception:
        return (0.0, 0.0)
    if a.size == 4:
        return ((a[1] + a[3]) / 2, (a[0] + a[2]) / 2)
    if a.size >= 8 and a.size % 2 == 0:
        return (float(a[1::2].mean()), float(a[0::2].mean()))
    return (0.0, 0.0)
