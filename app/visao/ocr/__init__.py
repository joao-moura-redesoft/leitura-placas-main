"""OCR — múltiplos engines com auto-instalação via pip.

Fachada do pacote: reexporta a API pública usada pelo resto do projeto.
Implementação dividida em:
  engines.py : classe OCR (por-engine: carregamento, pré-processamento, leitura)
  auto.py    : seleção automática (AutoOCR, AutoOCRPaddle) e votação (MultiOCR)
"""
from app.visao.ocr.engines import OCR, CHARS_VALIDOS
from app.visao.ocr.auto import (
    AutoOCR,
    AutoOCRPaddle,
    MultiOCR,
    obter_ocr_leitura,
    ocr_leitura_lock,
)

__all__ = [
    "OCR",
    "CHARS_VALIDOS",
    "AutoOCR",
    "AutoOCRPaddle",
    "MultiOCR",
    "obter_ocr_leitura",
    "ocr_leitura_lock",
]
