"""Ajustes de bibliotecas NATIVAS que precisam valer antes de qualquer trabalho de visão.

Módulo próprio, e importado o mais cedo possível, porque o que ele configura é estado
GLOBAL de bibliotecas C++ — aplicar depois de o primeiro frame passar pelo OpenCV não
desfaz o estado que já se formou.

## Por que existe

O log de produção de 24/08/2026 registra, num único processo:

    1030 x  "Unknown C++ exception from OpenCV code"
             (849 em `VehicleDetector: falha na inferência`,
              846 em `AjustadorAmbiente: falha ao processar frame`)
    2061 x  "Windows fatal exception" (dump do faulthandler)
       1 x  "access violation", dentro de `importlib._bootstrap_external._path_stat`

Contra 11.273 inferências de veículo bem-sucedidas — ou seja, não é falha constante. As
falhas ocupam uma janela de 3,5 min (17:42:40 a 17:46:03) que começa 19 ms depois de
`VehicleDetector carregado`, e não voltam nas 2h16 seguintes de log. Nessa janela o
detector de veículo simplesmente não funcionou, em silêncio: a exceção é capturada e vira
WARNING, e `deteccoes.tipo_veiculo` chega nulo, indistinguível de "não havia veículo".

A janela é exatamente a da carga concorrente de modelos. `servidor.lifespan` dispara
`_iniciar_pipeline_bg` (que abre câmeras e já roda CLAHE, bilateralFilter e inferência) e
`_aquecer_modelos_bg` (que cria sessões ONNX e importa EasyOCR/PaddleOCR) no MESMO
executor, ao mesmo tempo.

## O que cada ajuste resolve

`cv2.setNumThreads(0)` + `cv2.ocl.setUseOpenCL(False)` são exatamente o que
`testes/unitarios/conftest.py` já fazia — e o comentário de lá descreve este bug:
"um módulo que exercita o ajuste de ambiente (CLAHE, bilateralFilter) deixa o pool interno
do OpenCV num estado em que a PRÓXIMA chamada de `cv2.cvtColor`, noutro módulo, estoura com
'Unknown C++ exception from OpenCV code' — no Windows". A suíte estava protegida e o
servidor não, rodando o mesmo código no mesmo SO (`ajuste_ambiente = sim` é o default).

Não muda resultado numérico — é a mesma implementação do OpenCV, sem paralelismo interno —
mas NÃO é de graça. Medido em quadro 1280x720 de posto, no `AjustadorAmbiente` completo
(CLAHE + white balance + saturação + bilateralFilter), que é o consumidor mais pesado:

    threads=4 (o default do OpenCV) ..... 33,6 ms/quadro
    threads=2 ........................... 40,6 ms
    threads=1 ........................... 52,3 ms
    threads=0 (=1 efetivo, o que usamos)  53,5 ms

Desligar o OpenCL é GRATUITO (33,63 ms com OpenCL off contra 33,67 com on): todo o custo
acima é da redução de threads. Com as DUAS câmeras do posto processando em paralelo, num
host de 4 núcleos, a rodada completa mede 41,7 ms com threads=4 contra 57,2 ms com 0 — a
oversubscription que eu esperava (2 câmeras x 4 threads em 4 núcleos) não apareceu nessa
escala, então o custo é real e não se dilui.

Mesmo assim ficamos em 0, e a razão é o orçamento: `deteccao_fps_max` no posto é **1**, ou
seja 1000 ms por quadro por câmera. Os 57 ms são **5,7% do orçamento**, e os 15 ms de
diferença contra threads=4 são ruído ao lado de 849 quadros com o detector de veículo
morto. Trocar por 2 economizaria 10 ms de 1000 em troca de um palpite — ninguém mediu se
paralelismo PARCIAL ainda dispara o bug, e com 0 sabemos que não dispara.

`OPENCV_NUM_THREADS` existe para o dia em que `deteccao_fps_max` subir muito (a 10/s o
orçamento cai para 100 ms e a conta passa a ser outra) ou para um host com menos núcleo.
Mexer nele antes disso é otimizar 1% pagando em risco.

As variáveis de OpenMP (`OMP_NUM_THREADS` e cia.) NÃO são setadas aqui de propósito:
para valer, elas têm de existir antes de o runtime nativo carregar, e a essa altura
`import cv2` já aconteceu em algum lugar da cadeia de imports. O lugar delas é o ambiente
do processo — `entrypoint.sh` e `docker-compose*.yml`. Ver README.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

# 0 = sem paralelismo interno do OpenCV. Aceita override por ambiente para dar uma saída
# sem deploy caso o custo em FPS apareça em produção: `OPENCV_NUM_THREADS=1`.
try:
    LIMITE_THREADS = int(os.environ.get("OPENCV_NUM_THREADS", "0"))
except ValueError:
    LIMITE_THREADS = 0

_aplicado = False


def aplicar() -> None:
    """Idempotente e NUNCA levanta — é código de inicialização de biblioteca nativa.

    Uma falha aqui não pode impedir o servidor de subir: o pior caso de não conseguir
    aplicar é voltar ao comportamento que já existia, e uma exceção neste ponto derrubaria
    a aplicação inteira por causa de uma otimização de estabilidade.
    """
    global _aplicado
    if _aplicado:
        return
    _aplicado = True
    try:
        import cv2
    except Exception as e:                      # pragma: no cover - ambiente sem OpenCV
        log.warning("OpenCV indisponível (%s), ajustes nativos não aplicados", e)
        return

    try:
        cv2.setNumThreads(LIMITE_THREADS)
    except Exception as e:
        log.warning("cv2.setNumThreads(%d) falhou: %s", LIMITE_THREADS, e)

    try:
        cv2.ocl.setUseOpenCL(False)
    except Exception:
        pass          # build de OpenCV sem OpenCL — não há o que desligar

    # NAO loga aqui: `aplicar()` roda no IMPORT de `app.servidor`, antes de o `lifespan`
    # chamar `logging.basicConfig`. Sem handler, a linha era emitida e descartada - foi o
    # que aconteceu na primeira subida com a blindagem: os zeros de excecao provavam que
    # ela estava ativa, e o log nao tinha como confirmar. Quem loga e `estado_para_log()`,
    # chamado pelo `lifespan` depois que o logging existe.


def estado_para_log() -> str:
    """Estado efetivo, para o `lifespan` registrar DEPOIS de configurar o logging.

    Le do OpenCV em vez de repetir `LIMITE_THREADS`: o que interessa no diagnostico e o
    que ficou valendo, nao o que se pediu. `setNumThreads(0)` faz o getter devolver 1, e
    um build sem OpenCL nao tem o que desligar - as duas coisas so aparecem lendo de volta.
    """
    try:
        import cv2
        return "threads=%d opencl=%s (pedido=%d)" % (
            cv2.getNumThreads(), _opencl_ligado(), LIMITE_THREADS)
    except Exception as e:
        return "indisponivel (%s)" % e


def _opencl_ligado() -> bool | None:
    """`None` quando o build não tem OpenCL — o log distingue 'desligado' de 'inexistente'."""
    try:
        import cv2
        return bool(cv2.ocl.useOpenCL())
    except Exception:
        return None
