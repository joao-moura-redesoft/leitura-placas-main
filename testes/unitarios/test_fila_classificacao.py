"""Fila de classificação das capturas do ALPR (`/api/testes/candidatos`).

O ALPR captura muito mais do que alguém rotula: eram 429 capturas na fila contra 6 fotos
no dataset. A fila existe para transformar esse acúmulo em dataset — e o risco dela é
metodológico, não técnico: a placa vem no NOME do arquivo, mas é o que o OCR leu, não a
verdade. Se ela chegasse à tela como `placa_correta`, classificar viraria clicar "ok" e o
dataset passaria a medir o OCR contra ele mesmo — acurácia perto de 100% sem significar
nada, que é a mesma armadilha das fotos sintéticas que foram removidas do dataset.

Por isso os testes abaixo fixam, além do vai-e-vem da fila, que o campo se chama
`placa_sugerida` e que `preview_*` nunca entra (o arquivo é sobrescrito a cada leitura).
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

import app.web.testes as t


@pytest.fixture
def area(tmp_path, monkeypatch):
    """Isola dataset, descartados e pastas de imagem do repositório de verdade."""
    snaps = tmp_path / "snapshots"
    fotos = tmp_path / "fotos"
    snaps.mkdir()
    fotos.mkdir()
    monkeypatch.setattr(t, "_SNAPSHOTS", snaps)
    monkeypatch.setattr(t, "_FOTOS_TESTE", fotos)
    monkeypatch.setattr(t, "_DATASET", tmp_path / "dataset.json")
    monkeypatch.setattr(t, "_DESCARTADOS", tmp_path / "descartados.json")

    for nome in ("20260812T101010_ABC1D23.jpg",       # crop com placa lida
                 "20260812T101111_XYZ4567.jpg",
                 "20260812T101212_QRS8T90_frame.jpg",  # quadro inteiro
                 "preview_bico_3.jpg"):                # efêmero: sobrescrito a cada leitura
        (snaps / nome).write_bytes(b"jpg")
    return tmp_path


def _arquivos(resposta):
    return {c["arquivo"] for c in resposta["candidatos"]}


def test_preview_nunca_entra_na_fila(area):
    """`preview_bico_N.jpg` muda de conteúdo sozinho — rotulá-lo cria alvo móvel."""
    r = t.listar_candidatos()
    assert not any("preview_" in a for a in _arquivos(r))
    assert r["total"] == 3


def test_placa_vem_como_sugestao_nunca_como_verdade(area):
    """O nome do arquivo carrega a leitura do OCR; aceitá-la sem conferir corrompe a medição."""
    c = next(c for c in t.listar_candidatos()["candidatos"] if "ABC1D23" in c["arquivo"])
    assert c["placa_sugerida"] == "ABC1D23"
    assert "placa_detectada" not in c        # nome antigo não vaza
    assert "placa_correta" not in c          # e nunca se apresenta como gabarito


def test_rotulada_sai_da_fila(area):
    r = t.listar_candidatos()
    alvo = sorted(_arquivos(r))[0]

    t.adicionar_foto({"arquivo": alvo, "placa_correta": "ABC1D23"})

    depois = t.listar_candidatos()
    assert alvo not in _arquivos(depois)
    assert depois["total"] == r["total"] - 1
    assert depois["no_dataset"] == 1


def test_descartar_e_restaurar(area):
    alvo = sorted(_arquivos(t.listar_candidatos()))[0]

    t.descartar_candidato({"arquivo": alvo})
    r = t.listar_candidatos()
    assert alvo not in _arquivos(r) and r["descartados"] == 1

    t.restaurar_candidato({"arquivo": alvo})
    r = t.listar_candidatos()
    assert alvo in _arquivos(r) and r["descartados"] == 0


def test_descarte_sobrevive_a_recarga(area):
    """A lista precisa ser persistente: em memória, a fila reapareceria inteira."""
    alvo = sorted(_arquivos(t.listar_candidatos()))[0]
    t.descartar_candidato({"arquivo": alvo})

    assert json.loads((area / "descartados.json").read_text(encoding="utf-8"))["arquivos"] == [alvo]
    assert alvo not in _arquivos(t.listar_candidatos())


def test_descartar_duas_vezes_nao_duplica(area):
    alvo = sorted(_arquivos(t.listar_candidatos()))[0]
    t.descartar_candidato({"arquivo": alvo})
    t.descartar_candidato({"arquivo": alvo})
    assert t.listar_candidatos()["descartados"] == 1


def test_restaurar_o_que_nao_foi_descartado_e_404(area):
    with pytest.raises(HTTPException) as e:
        t.restaurar_candidato({"arquivo": "nao/existe.jpg"})
    assert e.value.status_code == 404


@pytest.mark.parametrize("layout", ["carro", "moto"])
def test_layout_e_campo_estruturado(area, layout):
    """Antes 'moto' só existia em texto livre em `obs`, e o relatório não conseguia
    separar a taxa de moto — que é justamente a que está em questão."""
    alvo = sorted(_arquivos(t.listar_candidatos()))[0]
    t.adicionar_foto({"arquivo": alvo, "placa_correta": "ABC1D23", "layout": layout})

    ent = next(f for f in t._ler_dataset()["fotos"] if f["arquivo"] == alvo)
    assert ent["layout"] == layout


def test_layout_invalido_e_recusado(area):
    """Valor inválido não pode virar categoria nova e silenciosa no relatório."""
    alvo = sorted(_arquivos(t.listar_candidatos()))[0]
    with pytest.raises(HTTPException) as e:
        t.adicionar_foto({"arquivo": alvo, "placa_correta": "ABC1D23", "layout": "caminhao"})
    assert e.value.status_code == 400


def test_sem_layout_nao_inventa_valor(area):
    """Omitir é honesto — o relatório mostra '?'. Assumir 'carro' seria rótulo falso."""
    alvo = sorted(_arquivos(t.listar_candidatos()))[0]
    t.adicionar_foto({"arquivo": alvo, "placa_correta": "ABC1D23"})

    ent = next(f for f in t._ler_dataset()["fotos"] if f["arquivo"] == alvo)
    assert "layout" not in ent
