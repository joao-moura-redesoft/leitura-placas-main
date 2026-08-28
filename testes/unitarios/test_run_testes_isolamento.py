"""Isolamento por subprocesso do harness de medição (`testes/run_testes.py`).

O harness carregava todos os engines no mesmo processo e morria nesta máquina — não por
conflito de biblioteca, como parecia pelo `OpenBLAS error`, mas por falta de RAM (7,8 GB
no total, ~0,9 GB livres): a mensagem real era "DefaultCPUAllocator: not enough memory"
ao pedir 9 MB. Enquanto morria, NENHUMA mudança de OCR podia ser medida no dataset.

O que estes testes fixam é o contrato do isolamento, não a acurácia: cada engine roda num
subprocesso, e um subprocesso que morre vira uma linha de falha no relatório em vez de
derrubar a medição dos outros. O caminho feliz é coberto rodando o harness de verdade
(`--comparar`, 4 engines); a morte do filho não dá para provocar sob demanda, por isso
aqui ela é simulada.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_RAIZ = Path(__file__).resolve().parents[2]


def _carregar_harness():
    """run_testes.py é script, não módulo — carrega pelo caminho, como a API web faz."""
    spec = importlib.util.spec_from_file_location(
        "run_testes_sob_teste", _RAIZ / "testes" / "run_testes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def harness():
    return _carregar_harness()


FOTOS = [{"arquivo": "a.jpg", "placa_correta": "ABC1D23"}] * 3


class ProcessoFalso:
    def __init__(self, returncode):
        self.returncode = returncode


@pytest.mark.parametrize("returncode, trecho", [
    (3221225477, "falta de memória"),    # 0xC0000005 como o Windows devolve (sem sinal)
    (-1073741819, "falta de memória"),   # o mesmo código com sinal, conforme a versão
    (-11, "falta de memória"),           # SIGSEGV (POSIX)
    (1, "código 1"),                     # exceção comum: não inventa causa de memória
])
def test_filho_que_morre_vira_linha_de_falha(harness, monkeypatch, returncode, trecho):
    """Morte do subprocesso não pode propagar: vira entrada de erro no relatório."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ProcessoFalso(returncode))

    r = harness._rodar_engine_isolado("easyocr", FOTOS, "leitura")

    assert trecho in r["erro"]
    assert r["ok"] == 0 and r["acuracia"] == 0
    assert r["total"] == len(FOTOS)      # o resumo comparativo lê estas chaves
    assert r["detalhes"] == []


def test_saida_ilegivel_nao_estoura(harness, monkeypatch):
    """Filho que retorna 0 mas grava lixo (buffer truncado) também é falha tratada."""
    def _run(cmd, **kwargs):
        Path(cmd[cmd.index("--saida-worker") + 1]).write_text("{trunca", encoding="utf-8")
        return ProcessoFalso(0)
    monkeypatch.setattr(subprocess, "run", _run)

    r = harness._rodar_engine_isolado("auto", FOTOS, "leitura")

    assert "ilegível" in r["erro"]
    assert r["total"] == len(FOTOS)


def test_um_engine_que_falha_nao_impede_os_outros(harness, monkeypatch):
    """É a promessa central: medir 4 engines não pode virar zero medições."""
    monkeypatch.setattr(harness, "_carregar_fotos", lambda: FOTOS)

    def _isolado(engine, fotos, caminho):
        if engine == "easyocr":
            return harness._engine_falhou(engine, len(fotos), -11, False)
        return {"acuracia": 0.5, "ok": 1, "erros": 1, "falhas_deteccao": 0,
                "total": 2, "por_formato": {}, "confusoes": {}, "detalhes": []}
    monkeypatch.setattr(harness, "_rodar_engine_isolado", _isolado)

    rel = harness.rodar(engines=["auto", "easyocr", "tesseract"])

    assert rel["engines"]["easyocr"]["erro"]
    assert rel["engines"]["auto"]["ok"] == 1
    assert rel["engines"]["tesseract"]["ok"] == 1


def test_arquivo_temporario_e_removido(harness, monkeypatch):
    """O harness roda em loop durante ajustes; não pode deixar lixo em %TEMP%."""
    vistos = []

    def _run(cmd, **kwargs):
        caminho = Path(cmd[cmd.index("--saida-worker") + 1])
        vistos.append(caminho)
        caminho.write_text(json.dumps({"ok": 0, "total": 0, "acuracia": 0}), encoding="utf-8")
        return ProcessoFalso(0)
    monkeypatch.setattr(subprocess, "run", _run)

    harness._rodar_engine_isolado("auto", FOTOS, "leitura")

    assert vistos and not vistos[0].exists()


def test_filho_recebe_em_processo(harness, monkeypatch):
    """Sem `--em-processo` o filho forkaria de novo — recursão infinita de subprocessos."""
    capturado = {}

    def _run(cmd, **kwargs):
        capturado["cmd"] = cmd
        Path(cmd[cmd.index("--saida-worker") + 1]).write_text("{}", encoding="utf-8")
        return ProcessoFalso(0)
    monkeypatch.setattr(subprocess, "run", _run)

    harness._rodar_engine_isolado("auto", FOTOS, "live")

    cmd = capturado["cmd"]
    assert "--em-processo" in cmd
    assert cmd[cmd.index("--caminho") + 1] == "live"     # o caminho chega ao filho
    assert cmd[cmd.index("--engine") + 1] == "auto"


def test_detector_so_carrega_se_houver_frame(harness, monkeypatch):
    """O dataset é quase todo `crop`; carregar o detector à toa é justamente o que estoura."""
    chamadas = []
    monkeypatch.setattr(harness, "_criar_detector",
                        lambda cfg, caminho: chamadas.append(caminho) or _DetectorFalso())

    det = harness._DetectorPreguicoso({}, "leitura")
    assert chamadas == []          # construir não carrega nada

    det.detectar(object())
    det.detectar(object())
    assert chamadas == ["leitura"]  # carrega uma vez só, no primeiro frame


class _DetectorFalso:
    def detectar(self, _img):
        return []
