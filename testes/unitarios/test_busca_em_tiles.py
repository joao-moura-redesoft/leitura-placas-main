"""Varredura em janelas — o fallback que recupera placa de MOTO parada na bomba.

Contexto medido em cena real (moto no bico 5, placa de 38x35px numa ROI de 397x610): o
detector de placa faz letterbox de tudo para 608px, então numa ROI grande a placa de moto
chega pequena demais e não sai em passada única — e ampliar a ROI não resolve (o modelo
reduz de volta), nem recortar fechado só na placa (o modelo precisa do veículo em volta).
O que recupera é variar o ENQUADRAMENTO, em janelas de ~250-320px.

Isso importa porque o loop de leitura repete no TEMPO mas não no ESPAÇO: com a ROI do bico
fixa, as 12 tentativas eram 12 recortes idênticos — 12 falhas idênticas para uma moto
parada. Os testes abaixo travam as duas metades disso: que a varredura ENTRE quando a
passada normal falha, e que ela NÃO entre (nem custe passadas extras) quando não falha.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.visao.detector import BuscaEmTiles

ROI = np.zeros((610, 397, 3), dtype=np.uint8)


class DetectorFake:
    """Devolve bboxes fixas e conta quantas vezes foi chamado e com que tamanhos."""

    def __init__(self, saidas=None):
        self.saidas = saidas if saidas is not None else []
        self.chamadas: list[tuple[int, int]] = []
        self.sess = "carregado"
        self.carregou = False

    def carregar(self):
        self.carregou = True

    def detectar(self, frame):
        self.chamadas.append((frame.shape[1], frame.shape[0]))
        return list(self.saidas)


def _busca(saidas_normal, saidas_tile, **kw):
    normal, tile = DetectorFake(saidas_normal), DetectorFake(saidas_tile)
    return BuscaEmTiles(normal, tile, **kw), normal, tile


def test_nao_varre_quando_a_passada_normal_acha():
    """O caso comum (carro): custo extra tem que ser ZERO, não só pequeno.

    A varredura gasta até `max_janelas` passadas do detector (~200ms cada em CPU). Se ela
    rodasse também quando o caminho normal já achou a placa, toda leitura de carro pagaria
    essa latência à toa — e o GET tem orçamento de tempo (`leitura_timeout_seg`).
    """
    busca, normal, tile = _busca([(10, 20, 100, 40, 0.9)], [(0, 0, 5, 5, 0.5)])

    assert busca.detectar(ROI) == [(10, 20, 100, 40, 0.9)]
    assert len(normal.chamadas) == 1
    assert tile.chamadas == []


def test_varre_em_janelas_quando_a_passada_normal_nao_acha():
    busca, normal, tile = _busca([], [(30, 40, 38, 35, 0.32)])

    achados = busca.detectar(ROI)

    assert len(normal.chamadas) == 1
    assert len(tile.chamadas) == 6
    # Toda janela cai na faixa de ~250-320px que funciona na cena real — se a geometria
    # derivar para janelas do tamanho da ROI, volta a ser a passada que já falhou.
    for w, h in tile.chamadas:
        assert 240 <= max(w, h) <= 330, f"janela {w}x{h} fora da faixa medida"
    assert achados


def test_coordenadas_voltam_para_o_recorte_analisado():
    """Sem o deslocamento da janela, a bbox aponta para o lugar errado do frame.

    Quem chama usa essa bbox para cortar o crop do OCR e para desenhar o preview: uma
    bbox em coordenadas de janela recortaria pedaço de asfalto, não a placa.
    """
    busca, _, tile = _busca([], [(30, 40, 38, 35, 0.32)])

    achados = busca.detectar(ROI)

    janelas = busca._janelas(397, 610)
    esperadas = {(x0 + 30, y0 + 40, 38, 35, 0.32) for x0, y0, _x1, _y1 in janelas}
    # _dedup remove as sobrepostas, então o que sobra é subconjunto — o que importa é que
    # toda bbox devolvida corresponda a uma janela deslocada.
    assert achados
    assert set(achados) <= esperadas
    # E que alguma tenha REALMENTE sido deslocada: se o offset fosse esquecido, todas
    # sairiam na coordenada crua da janela.
    assert any(a[:2] != (30, 40) for a in achados)


def test_placa_na_divisa_entre_janelas_nao_duplica():
    """A sobreposição existe para não partir uma placa na divisa; o preço é vê-la 2x.

    Sem dedup, a mesma placa entraria como 2 candidatos no consenso de `_eleger_placa`,
    inflando artificialmente o "acordo" e podendo fazer o loop parar antes da hora.
    """
    # Bbox no mesmo lugar relativo em toda janela: as janelas sobrepostas geram
    # duplicatas quase coincidentes.
    busca, _, _tile = _busca([], [(0, 0, 200, 200, 0.5)])

    achados = busca.detectar(ROI)

    assert achados
    for i, a in enumerate(achados):
        for b in achados[i + 1:]:
            ax, ay, aw, ah = a[:4]
            bx, by, bw, bh = b[:4]
            ix1, iy1 = max(ax, bx), max(ay, by)
            ix2, iy2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
            inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
            iou = inter / (aw * ah + bw * bh - inter)
            assert iou < 0.5, f"duplicata sobreviveu: {a} vs {b} (IoU={iou:.2f})"


@pytest.mark.parametrize("w,h", [(300, 300), (200, 250), (64, 64), (299, 299)])
def test_recorte_pequeno_nao_gera_varredura(w, h):
    """Recorte que já cabe numa janela: varrer seria repetir a passada que falhou.

    Aqui a passada única não é limitada por escala — é o recorte inteiro que o modelo já
    viu. Repetir custaria latência para produzir exatamente o mesmo nada.
    """
    busca, _, tile = _busca([], [(1, 1, 10, 10, 0.9)])

    assert busca.detectar(np.zeros((h, w, 3), dtype=np.uint8)) == []
    assert tile.chamadas == []


def test_respeita_o_teto_de_janelas():
    """`max_janelas` é o freio de latência — e engrossar é melhor que deixar buraco.

    Um frame grande com `lado_alvo` pequeno pediria dezenas de janelas. Cortar a lista
    deixaria parte do recorte sem varrer justamente onde a placa pode estar; a classe
    reduz o número de divisões, cobrindo tudo com janelas maiores.
    """
    busca, _, tile = _busca([], [], lado_alvo=100, max_janelas=4)

    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    busca.detectar(frame)

    assert 1 < len(tile.chamadas) <= 4
    # Cobertura: a união das janelas tem que alcançar as duas pontas do frame.
    janelas = busca._janelas(1280, 720)
    assert min(x0 for x0, _, _, _ in janelas) == 0
    assert min(y0 for _, y0, _, _ in janelas) == 0
    assert max(x1 for _, _, x1, _ in janelas) == 1280
    assert max(y1 for _, _, _, y1 in janelas) == 720


def test_carregar_propaga_para_os_dois_detectores():
    busca, normal, tile = _busca([], [])

    busca.carregar()

    assert normal.carregou and tile.carregou
    # `estado.modelo_carregado` lê .sess — tem que refletir o detector principal.
    assert busca.sess == "carregado"


def test_sobreposicao_e_limitada_a_faixa_sa():
    """Sobreposição ≥ 1 faria a janela crescer sem limite (e virar o recorte inteiro)."""
    busca, _, _ = _busca([], [], sobreposicao=5.0)
    assert busca.sobreposicao <= 0.9

    busca, _, _ = _busca([], [], sobreposicao=-1.0)
    assert busca.sobreposicao == 0.0
