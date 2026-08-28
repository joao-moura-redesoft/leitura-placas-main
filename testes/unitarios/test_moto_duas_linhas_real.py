"""As duas motos reais que o sistema errou em 24/08/2026, no bico 3 do ALTIPLANO.

| gravado   | verdade   | placa                                    |
|-----------|-----------|------------------------------------------|
| `HDX2477` | `RLX2A77` | Mercosul de moto, faixa azul, 2 linhas   |
| `OSL2855` | `OSL2659` | antiga de moto, cinza metálica, 2 linhas |

As duas eram legíveis e as duas foram destruídas por lógica de pipeline, não por limitação
de OCR: o sistema chegou a ler `RLX2A77` com confiança 0,96 e todos os `char_probs` ≥ 0,93
e emitiu `HDX2477`; e emitiu `OSL2855`, string que engine NENHUM produziu, com acordo 0,00.

Este é o teste de aceitação da mudança para ensemble + fusão por caractere. Ele roda os
modelos de verdade, então é lento e depende dos `.onnx` em cache — por isso o `skipif`.

Os recortes vivem em `testes/reais/`, gitignored de propósito (contém placa e veículo de
clientes reais). Mesmo padrão de `testes/avalia_cenas_reais.py`: o teste lê do disco local
e nunca copia nada para dentro do repositório. Em máquina que não tem os arquivos ele é
pulado, e não falha.

`testes/reais/` e não `app/web/static/snapshots/`, que é onde eles nasceram: aquela pasta
passou a ter teto de contagem (`retencao_max_imagens`), e a purga apagaria a entrada deste
teste. O `skipif` abaixo é o que torna isso perigoso — o teste não ficaria vermelho, ele
sumiria em silêncio, e a regressão de moto voltaria a passar despercebida. Aqui os arquivos
estão fora do alcance da purga por construção, não por lista de exceção.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REAIS = Path("testes/reais")
# Fallback para o lugar antigo: numa máquina que ainda não moveu os arquivos o teste
# continua rodando em vez de sumir. `testes/reais/` tem precedência — é o único dos dois
# que a purga não alcança.
_LEGADO = Path("app/web/static/snapshots")

CASOS = [
    ("20260824T205314_HDX2477.jpg", "RLX2A77", "Mercosul de moto (faixa azul)"),
    ("20260824T205425_OSL2855.jpg", "OSL2659", "antiga de moto (cinza metalica)"),
]


def caminho(nome: str) -> Path:
    return REAIS / nome if (REAIS / nome).exists() else _LEGADO / nome


_faltando = [n for n, _, _ in CASOS if not caminho(n).exists()]
pytestmark = pytest.mark.skipif(
    bool(_faltando),
    reason="recortes reais ausentes (gitignored): %s" % ", ".join(_faltando),
)


@pytest.fixture(scope="module")
def ocr():
    """Ensemble completo do GET, uma vez para o módulo — carregar custa segundos."""
    cv2 = pytest.importorskip("cv2")
    pytest.importorskip("fast_plate_ocr")
    from app.visao.ocr.auto import AutoOCR

    o = AutoOCR()          # 3 modelos do fast-plate-ocr, sem EasyOCR (o default medido)
    o.carregar()
    assert len(o._fast._fast_membros) >= 2, (
        "o ensemble subiu com %d membro(s) — este teste não mede nada com um só"
        % len(o._fast._fast_membros)
    )
    return o


@pytest.mark.parametrize("arquivo, esperada, descricao", CASOS,
                         ids=[c[1] for c in CASOS])
def test_le_a_placa_de_moto_de_duas_linhas(ocr, arquivo, esperada, descricao):
    import cv2

    crop = cv2.imread(str(caminho(arquivo)))
    assert crop is not None, "recorte ilegível: %s" % arquivo

    d = ocr.ler_detalhado(crop)
    individuais = [x["placa"] for x in d["detalhes"]]
    assert d["placa"] == esperada, (
        "%s (%s): esperado %s, veio %s. Leituras individuais: %s"
        % (arquivo, descricao, esperada, d["placa"], individuais)
    )


def test_a_fusao_e_o_que_acerta_e_nao_um_modelo_sortudo(ocr):
    """Se um único membro já acertasse as duas, o ensemble não estaria se pagando.

    Este teste existe para o caso de alguém "simplificar" a fusão de volta para um modelo:
    ele documenta que, nas duas placas, os membros DISCORDAM entre si e a resposta certa é
    o voto, não a leitura de ninguém em particular.
    """
    import cv2

    houve_discordancia = False
    for arquivo, esperada, _ in CASOS:
        crop = cv2.imread(str(caminho(arquivo)))
        d = ocr.ler_detalhado(crop)
        lidas = {x["placa"] for x in d["detalhes"] if x["placa"]}
        if len(lidas) > 1:
            houve_discordancia = True
    assert houve_discordancia, (
        "todos os membros concordaram nas duas placas — reveja se o ensemble está mesmo "
        "rodando com mais de um modelo, ou se este teste ainda mede o que diz medir"
    )
