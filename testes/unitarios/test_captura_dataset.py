"""Captura de imagens para o dataset — o que o histórico descarta.

O pipeline só grava snapshot quando a leitura dá certo, então a base cresce contendo
apenas o que o sistema já acerta. O operador revisou 74 capturas automáticas em
12/08/2026 e não achou UMA moto, enquanto o dataset seguia com 2 — não é azar, é o
gatilho: placa de moto que não é detectada, ou é detectada e não é lida, nunca vira
arquivo.

Estes testes fixam os dois gatilhos novos (amostra periódica e negativo), os limites que
impedem a coleta de encher o disco, e a garantia de que o nome gerado NUNCA é lido como
placa — se fosse, a fila de classificação sugeriria uma placa inventada pelo nome do
arquivo, que é exatamente o tipo de rótulo falso que corrompe a medição.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.visao.captura_dataset import CapturaDataset
from app.web.testes import _placa_do_nome

IMG = np.zeros((80, 200, 3), dtype=np.uint8)


@pytest.fixture
def dir_snap(tmp_path, monkeypatch):
    import app.visao.captura_dataset as mod
    monkeypatch.setattr(mod, "SNAPSHOT_DIR", tmp_path)
    return tmp_path


def _cap(dir_snap, **cfg):
    base = {"captura_dataset": "sim", "captura_dataset_intervalo_seg": "0",
            "captura_dataset_negativo_intervalo_seg": "0"}
    base.update(cfg)
    return CapturaDataset(base, camera_db_id=3)


def _arquivos(d):
    return sorted(f.name for f in d.iterdir())


def test_desligado_por_padrao(dir_snap):
    """Coletar custa disco e enche a fila — só liga quem quer montar base."""
    c = CapturaDataset({}, camera_db_id=3)
    c.amostrar(IMG)
    c.negativo(IMG)
    assert _arquivos(dir_snap) == []


def test_amostra_grava_o_quadro(dir_snap):
    _cap(dir_snap).amostrar(IMG)
    assert len(_arquivos(dir_snap)) == 1
    assert "-amostra." in _arquivos(dir_snap)[0]


def test_negativo_grava_o_recorte(dir_snap):
    _cap(dir_snap).negativo(IMG)
    assert "-naolido." in _arquivos(dir_snap)[0]


@pytest.mark.parametrize("marca", ["amostra", "naolido"])
def test_nome_nunca_e_lido_como_placa(dir_snap, marca):
    """A fila extrai `placa_sugerida` do nome do arquivo. Um nome que casasse com o
    padrão de placa faria a tela sugerir uma placa que ninguém leu."""
    c = _cap(dir_snap)
    c.amostrar(IMG) if marca == "amostra" else c.negativo(IMG)
    nome = _arquivos(dir_snap)[0]
    assert _placa_do_nome(nome) == ""


def test_intervalo_limita_a_amostragem(dir_snap):
    """Sem intervalo, a amostra gravaria a cada tick de detecção (varios por segundo)."""
    c = _cap(dir_snap, captura_dataset_intervalo_seg="3600")
    for _ in range(5):
        c.amostrar(IMG)
    assert len(_arquivos(dir_snap)) == 1


def test_negativo_tem_intervalo_proprio(dir_snap):
    """Caixa fantasma fixa na cena (adesivo, placa de sinalizacao) gravaria sem parar."""
    c = _cap(dir_snap, captura_dataset_negativo_intervalo_seg="3600")
    for _ in range(5):
        c.negativo(IMG)
    assert len(_arquivos(dir_snap)) == 1


def test_negativo_pode_ser_desligado_sem_desligar_a_amostra(dir_snap):
    c = _cap(dir_snap, captura_dataset_negativos="nao")
    c.negativo(IMG)
    assert _arquivos(dir_snap) == []
    c.amostrar(IMG)
    assert len(_arquivos(dir_snap)) == 1


def test_teto_de_arquivos_para_a_coleta(dir_snap):
    """Ao bater o teto a coleta PARA. Não apaga: apagar arriscaria remover snapshot
    que uma detecção do histórico referencia."""
    for i in range(3):
        (dir_snap / f"ja_existia_{i}.jpg").write_bytes(b"x")
    c = _cap(dir_snap, captura_dataset_max_arquivos="3")

    c.amostrar(IMG)

    assert len(_arquivos(dir_snap)) == 3     # nada foi gravado nem apagado


def test_teto_zero_significa_sem_limite(dir_snap):
    c = _cap(dir_snap, captura_dataset_max_arquivos="0")
    c.amostrar(IMG)
    assert len(_arquivos(dir_snap)) == 1


def test_falha_de_escrita_nao_derruba_o_pipeline(dir_snap, monkeypatch):
    """Isto é coleta acessória: nunca pode interromper a operação da câmera."""
    import app.visao.captura_dataset as mod
    monkeypatch.setattr(mod.cv2, "imwrite", lambda *a, **k: (_ for _ in ()).throw(OSError("disco cheio")))

    _cap(dir_snap).amostrar(IMG)             # não levanta

    assert _arquivos(dir_snap) == []


def test_crop_vazio_e_ignorado(dir_snap):
    _cap(dir_snap).negativo(np.zeros((0, 0, 3), dtype=np.uint8))
    assert _arquivos(dir_snap) == []
