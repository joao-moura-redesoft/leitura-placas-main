"""Detecção de hardware disponível (GPU) para acelerar detecção/OCR.

Este ambiente de desenvolvimento roda em CPU, mas o servidor de produção tem GPU.
Os componentes pedem CUDA automaticamente quando disponível (sem precisar trocar
código ao migrar) — basta instalar os pacotes com suporte a GPU no servidor de
destino (ex.: `onnxruntime-gpu` no lugar de `onnxruntime`, `torch` com build CUDA).
Se a GPU não estiver disponível, cai para CPU sem erro.
"""
from __future__ import annotations
import logging
import os

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
            log.info("ONNX Runtime: GPU CUDA disponível, detecção acelerada por GPU")
        else:
            _onnx_providers = ["CPUExecutionProvider"]
            log.info("ONNX Runtime: sem CUDA (pacote onnxruntime-gpu ausente ou sem GPU), usando CPU")
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
        log.info("PyTorch (EasyOCR): GPU CUDA %s", "disponível" if _torch_cuda else "indisponível, usando CPU")
    return _torch_cuda


def onnx_session_options():
    """`SessionOptions` com o paralelismo interno do ORT sob controle.

    `OMP_NUM_THREADS` **não** governa o onnxruntime: ele tem pool próprio e, sem
    `intra_op_num_threads`, dimensiona pelo total de núcleos do HOST — inclusive os que o
    `cpus:` do compose não deixa usar. O container fica pedindo mais CPU do que tem e o
    cgroup responde com throttle, então o efeito é perda de vazão, não ganho.

    Medido nesta máquina (4 núcleos, YOLOX-s 640x640, `OMP_NUM_THREADS=1` já ativo):

        1 sessão   sem opts ...... 470,0 ms/infer   2,19 núcleos
        1 sessão   intra=1 ....... 428,3 ms/infer   0,87 núcleo
        2 sessões  sem opts ...... 2,31 infer/s     2,84 núcleos
        2 sessões  intra=1 ....... 3,66 infer/s     1,53 núcleo

    Com as DUAS câmeras do posto o explícito é 58% mais rápido gastando METADE da CPU: o
    default abre 2 pools de 4 threads em 4 núcleos e o tempo vai para troca de contexto.
    Não é troca de precisão por velocidade — é a mesma inferência, o mesmo resultado.

    `ONNX_INTRA_THREADS` dá a saída sem deploy para o host com muito núcleo, onde subir
    para 2 pode compensar (aqui rendeu 311 ms/infer, mas a 1,52 núcleo — só vale quando
    há núcleo sobrando de verdade). 0 = deixa o ORT decidir (comportamento antigo).
    """
    try:
        import onnxruntime as ort
    except Exception:                            # pragma: no cover - ambiente sem ORT
        return None
    try:
        intra = int(os.environ.get("ONNX_INTRA_THREADS", "1"))
    except ValueError:
        intra = 1
    if intra <= 0:
        return None                              # explicitamente "deixa o ORT decidir"
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = intra
    # Os nossos grafos são sequenciais (1 imagem por chamada) — não há ramo paralelo para
    # o inter-op explorar, então >1 aqui só criaria threads ociosas.
    opts.inter_op_num_threads = 1
    return opts
