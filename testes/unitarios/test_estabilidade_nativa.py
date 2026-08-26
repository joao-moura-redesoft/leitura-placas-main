"""Blindagem de biblioteca nativa e alarme de componente inoperante.

O log de produção de 24/08/2026 registra, num único processo:

    1030 x  "Unknown C++ exception from OpenCV code"
             (849 em `VehicleDetector: falha na inferência`,
              846 em `AjustadorAmbiente: falha ao processar frame`)
    2061 x  "Windows fatal exception" (dump do faulthandler)
       1 x  "access violation", dentro de `importlib._bootstrap_external._path_stat`

Contra 11.273 inferências bem-sucedidas: não é falha constante. As falhas ocupam a janela
de 3,5 min em que `servidor.lifespan` carregava modelos EM PARALELO com o pipeline de
câmera já rodando CLAHE e inferência. Nessa janela o detector de veículo estava morto e
`deteccoes.tipo_veiculo` chegou nulo ao banco, indistinguível de "não havia veículo".

O que este arquivo guarda são as três defesas que saíram daquela investigação, e nenhuma
delas é observável rodando o servidor uma vez com sorte — daí o teste.
"""
from __future__ import annotations

import logging

import cv2
import pytest

from app.visao.contexto_log import FALHAS_PARA_ALARMAR, ContadorDeFalhas


class TestBlindagemOpenCV:
    """`cv2.setNumThreads(0)` + `ocl.setUseOpenCL(False)` no processo do SERVIDOR.

    Vivia só em `testes/unitarios/conftest.py`, e o comentário de lá já descrevia o bug que
    produção estava tomando. A suíte protegida e o servidor não, rodando o mesmo código no
    mesmo SO — `ajuste_ambiente = sim` é o default e é ele que chama CLAHE/bilateralFilter.
    """

    def test_aplicar_desliga_threading_e_opencl(self):
        """RELIGA o paralelismo antes de medir — sem isso o teste é tautológico.

        `testes/unitarios/conftest.py` já chama `cv2.setNumThreads(0)` no import, para toda
        a suíte. Um assert direto em `getNumThreads()` passaria com `nativo.aplicar()`
        VAZIO. Religar para 4 e conferir que `aplicar()` derruba de volta é o que de fato
        exercita a função.
        """
        from app.core import nativo

        cv2.setNumThreads(4)
        cv2.ocl.setUseOpenCL(True)
        assert cv2.getNumThreads() == 4, "pré-condição: o teste precisa começar sujo"

        nativo._aplicado = False        # `aplicar` é idempotente por design
        nativo.aplicar()

        # OpenCV reporta 1 depois de `setNumThreads(0)`: zero significa "sem paralelismo" e
        # o getter devolve o número efetivo de threads, que é um.
        assert cv2.getNumThreads() <= 1
        assert cv2.ocl.useOpenCL() is False

    def test_e_idempotente(self):
        """Segunda chamada não refaz o trabalho (import duplo, subprocess do --reload)."""
        from app.core import nativo

        nativo._aplicado = False
        nativo.aplicar()
        cv2.setNumThreads(4)            # alguém mexeu depois
        nativo.aplicar()                # já aplicado: não desfaz nem estoura
        assert cv2.getNumThreads() == 4

    def test_importar_o_servidor_ja_aplica(self):
        """A blindagem vale por IMPORTAR `app.servidor`, não por chamar o lifespan.

        Em SUBPROCESSO, porque dentro da suíte o `conftest` já aplicou o mesmo ajuste e o
        assert passaria de graça. O `lifespan` roda depois de `app.visao` ter sido
        importado, e o subprocess do `--reload` nem passa pelo `main.py` — se alguém mover
        o `nativo.aplicar()` para dentro do lifespan "para organizar", este teste cai.
        """
        import subprocess
        import sys

        codigo = (
            "import app.servidor, cv2;"
            "print(cv2.getNumThreads(), cv2.ocl.useOpenCL())"
        )
        r = subprocess.run([sys.executable, "-c", codigo],
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-2000:]
        threads, opencl = r.stdout.strip().split()[-2:]
        assert int(threads) <= 1, "importar app.servidor não blindou o OpenCV"
        assert opencl == "False"


class TestContadorDeFalhas:
    """849 WARNINGs iguais não informam; um ERROR informa."""

    def test_falha_isolada_nao_alarma(self, caplog):
        c = ContadorDeFalhas("X", limiar=3)
        with caplog.at_level(logging.DEBUG):
            c.falhou("boom")
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_alarma_uma_vez_ao_cruzar_o_limiar(self, caplog):
        c = ContadorDeFalhas("VehicleDetector", limiar=3)
        with caplog.at_level(logging.DEBUG):
            for _ in range(10):
                c.falhou("Unknown C++ exception from OpenCV code")

        erros = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(erros) == 1, "dez falhas seguidas têm de dar UM erro, não dez"
        assert "INOPERANTE" in erros[0].getMessage()

    def test_recuperacao_e_logada(self, caplog):
        """Sem isto, quem vê o ERROR não sabe se durou 2 segundos ou 2 horas."""
        c = ContadorDeFalhas("X", limiar=2)
        with caplog.at_level(logging.DEBUG):
            c.falhou("a")
            c.falhou("b")
            c.funcionou()

        avisos = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert avisos and "VOLTOU" in avisos[0].getMessage()

    def test_sucesso_zera_a_contagem(self, caplog):
        """Falhas intercaladas com sucesso não são "falhas seguidas"."""
        c = ContadorDeFalhas("X", limiar=3)
        with caplog.at_level(logging.DEBUG):
            for _ in range(20):
                c.falhou("a")
                c.funcionou()
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_recuperacao_sem_alarme_e_silenciosa(self, caplog):
        c = ContadorDeFalhas("X", limiar=5)
        with caplog.at_level(logging.DEBUG):
            c.falhou("a")
            c.funcionou()
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_limiar_default_documentado(self):
        """O número está no comentário como "~2 s de vídeo a 5 fps" — se mudar, que seja
        de propósito."""
        assert FALHAS_PARA_ALARMAR == 10
        assert ContadorDeFalhas("X").limiar == 10

    def test_limiar_zero_nao_alarma_no_primeiro_sucesso(self):
        """`max(1, ...)`: limiar 0 seria "alarma antes de falhar"."""
        assert ContadorDeFalhas("X", limiar=0).limiar == 1


class TestDetectoresUsamOContador:
    """A ligação entre o helper e os dois detectores — o que o log de produção mostrou
    quebrado. Testa a FIAÇÃO, não o reconhecimento."""

    def test_vehicle_detector_alarma_em_falha_repetida(self, caplog, monkeypatch):
        import numpy as np

        from app.visao.detector import VehicleDetector

        d = VehicleDetector.__new__(VehicleDetector)
        d.sess = object()                     # finge modelo carregado
        d._falhas = ContadorDeFalhas("VehicleDetector", limiar=3)
        monkeypatch.setattr(
            VehicleDetector, "_inferir",
            lambda self, frame: (_ for _ in ()).throw(
                cv2.error("Unknown C++ exception from OpenCV code")),
        )
        frame = np.zeros((64, 64, 3), dtype=np.uint8)

        with caplog.at_level(logging.DEBUG):
            for _ in range(6):
                assert d.detectar(frame) == []

        erros = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(erros) == 1
        assert "VehicleDetector INOPERANTE" in erros[0].getMessage()

    def test_ajustador_ambiente_tem_contador(self):
        """O `AjustadorAmbiente` respondia por 846 dos WARNINGs."""
        from app.visao.ambiente import AjustadorAmbiente

        a = AjustadorAmbiente({"ajuste_ambiente": "sim"}, camera_db_id=3)
        assert isinstance(a._falhas, ContadorDeFalhas)
        assert "cam3" in a._falhas.nome


class TestStartupNaoCarregaModeloEmParalelo:
    """A causa raiz: `lifespan` disparava aquecer-modelos e subir-pipeline no MESMO
    executor, ao mesmo tempo. Agora é uma sequência.

    Teste sobre o FONTE de propósito: exercitar o lifespan de verdade sobe câmera e carrega
    modelo (dezenas de segundos), que é justamente o que o `conftest` evita ao criar o
    TestClient sem `with`.
    """

    def test_pipeline_sobe_depois_do_aquecimento(self):
        import inspect

        from app import servidor

        fonte = inspect.getsource(servidor.lifespan)
        assert "_subir_visao" in fonte, "a sequência foi removida"
        i_aquecer = fonte.index("_aquecer_modelos_bg")
        i_pipeline = fonte.index("_iniciar_pipeline_bg")
        assert i_aquecer < i_pipeline, (
            "aquecer modelos tem de vir ANTES de subir o pipeline: carregar sessão ONNX "
            "enquanto outro thread roda CLAHE foi o que gerou 849 falhas de inferência"
        )

    @pytest.mark.parametrize("proibido", ["_tarefa_modelos", "_tarefa ="])
    def test_nao_ha_mais_duas_tarefas_soltas(self, proibido):
        import inspect

        from app import servidor

        assert proibido not in inspect.getsource(servidor.lifespan)
