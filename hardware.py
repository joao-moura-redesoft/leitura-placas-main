"""Detecção de hardware disponível (GPU) para acelerar detecção/OCR.

Este ambiente de desenvolvimento roda em CPU, mas o servidor de produção tem GPU.
Os componentes pedem CUDA automaticamente quando disponível (sem precisar trocar
código ao migrar) — basta instalar os pacotes com suporte a GPU no servidor de
destino (ex.: `onnxruntime-gpu` no lugar de `onnxruntime`, `torch` com build CUDA).
Se a GPU não estiver disponível, cai para CPU sem erro.
"""
from __future__ import annotations
import logging

log = logging.getLogger(__name__)

_onnx_providers: list[str] | None = None
_torch_cuda: bool | None = None


def onnx_providers() -> list[str]:
    """Providers do ONNX Runtime, CUDA primeiro se disponível (com fallback p/ CPU).

    Cacheado — a checagem só roda uma vez por processo. Requer o pacote
    `onnxruntime-gpu` instalado (e driver/CUDA no SO) para a GPU ser detectada;
    com o `onnxruntime` comum, `CUDAExecutionProvider` não aparece e cai para CPU.
    """
    global _onnx_providers
    if _onnx_providers is None:
        try:
            import onnxruntime as ort
            disponiveis = ort.get_available_providers()
        except Exception:
            disponiveis = []
        if "CUDAExecutionProvider" in disponiveis:
            _onnx_providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
            log.info("ONNX Runtime: GPU CUDA disponível — detecção acelerada por GPU")
        else:
            _onnx_providers = ["CPUExecutionProvider"]
            log.info("ONNX Runtime: sem CUDA (pacote onnxruntime-gpu ausente ou sem GPU) — usando CPU")
    return _onnx_providers


def torch_cuda_disponivel() -> bool:
    """True se o PyTorch enxerga uma GPU CUDA utilizável. Cacheado por processo."""
    global _torch_cuda
    if _torch_cuda is None:
        try:
            import torch
            _torch_cuda = bool(torch.cuda.is_available())
        except Exception:
            _torch_cuda = False
        log.info("PyTorch (EasyOCR): GPU CUDA %s", "disponível" if _torch_cuda else "indisponível — usando CPU")
    return _torch_cuda
