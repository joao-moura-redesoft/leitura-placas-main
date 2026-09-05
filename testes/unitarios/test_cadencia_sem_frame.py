"""A cadência por fonte vale mesmo quando a câmera NUNCA publica frame.

O laço reject-retry tinha a espera guardada por `f.tentativas`, que só incrementa quando
um frame chega. Uma câmera reconectando (ou com frame velho demais) ficava em zero para
sempre, e o `time.sleep(0.1)` do caminho `frame is None` não cobria o caso porque é
guardado por `len(ativas) == 1`. Com DUAS fontes, ninguém freava o laço.

Medido nesta máquina com o `frame_ao_vivo` de verdade, orçamento de 5 s:

    1 câmera ......         50 chamadas ao provider    0,00 núcleo
    2 câmeras ..... 2.070.246 chamadas ao provider    0,98 núcleo

Um núcleo inteiro queimado justamente quando a câmera já está com problema, mais ~1,3
milhão de linhas de WARNING (~120 MB) numa única leitura — o bastante para estourar a
rotação de 40 MB do `alpr.log` e levar embora o rastro de qualquer queda.

A correção troca a guarda para `f.ultimo_ts`, que é atualizado em TODA passada do laço
(com ou sem frame). O dado já existia; só não estava sendo consultado.
"""
from __future__ import annotations

import time

import numpy as np
import pytest

from app.visao import leitura as leitura_mod

from test_payload_leitura import (CFG, BICO_ID, _DetectorFalso, _OcrFalso,  # noqa: F401
                                  _especificacao, visao_falsa)


class _ProviderMudo:
    """Câmera que aceitou o pipeline mas nunca entrega frame (reconectando).

    Devolve `None` DE GRAÇA, sem dormir — que é o que o `frame_ao_vivo` real faz nas
    chamadas seguintes à primeira, quando `limite` já expirou (o `primeira[0]` virou
    False e o teto vai a zero). É esse retorno instantâneo que fazia o laço girar.
    """

    def __init__(self):
        self.chamadas = 0

    def __call__(self):
        self.chamadas += 1
        return None


@pytest.fixture
def pipeline_vivo(monkeypatch):
    """Sondagem dublada: o pipeline EXISTE e está vivo, só não publica frame.

    Sem isto, `_abrir_fontes` roda a sondagem de verdade, vê `frame_inicial is None` e
    condena a fonte à conexão RTSP direta — que numa suíte sem câmera falha na hora e faz
    o laço nem começar (medido: 1 chamada ao provider e 503). O cenário que se quer medir
    é justamente o oposto: a fonte FICA no caminho do pipeline e o laço gira nela.
    """
    def _sonda(f, cfg):
        f.usar_pipeline = True
        f.pipeline_sondado = True
        f.frame_inicial = None

    monkeypatch.setattr(leitura_mod, "_sondar_pipeline", _sonda)


def _fonte(camera_id, provider):
    return leitura_mod.FonteLeitura(
        camera_id=camera_id, papel="traseira", especificacao=_especificacao(),
        roi=None, provider=provider)


def _ler_com(fontes, timeout=2.0):
    """Roda o laço e devolve quanto tempo levou. Aceita os DOIS desfechos.

    Sem frame nenhum sai 503; com frames sai dict. Qual dos dois não é o que estes testes
    medem — eles medem a CADÊNCIA — então engolir a `LeituraError` aqui evita que cada
    teste tenha de saber de antemão qual caminho o seu cenário toma.
    """
    cfg = {**CFG, "leitura_timeout_seg": str(timeout)}
    t0 = time.time()
    try:
        leitura_mod.ler_placa(fontes=fontes, cfg=cfg, preview_nome="x",
                              bico_id=BICO_ID, origem="teste")
    except leitura_mod.LeituraError:
        pass
    return time.time() - t0


class TestNaoGiraLivre:

    @pytest.mark.parametrize("n_fontes", [1, 2, 3])
    def test_provider_e_chamado_em_cadencia(self, ambiente, visao_falsa,
                                            pipeline_vivo, n_fontes):
        """Conta CHAMADAS num orçamento fixo, não taxa por segundo.

        O intervalo do pipeline é 0,15s, então 2s de orçamento cabem ~13 chamadas por
        fonte. O teto de 40 por fonte deixa folga enorme para máquina lenta e ainda falha
        por quatro ordens de grandeza sem a correção (foram ~1 milhão por fonte).
        """
        visao_falsa(_DetectorFalso([]), _OcrFalso())
        provs = [_ProviderMudo() for _ in range(n_fontes)]
        fontes = [_fonte(10 + i, p) for i, p in enumerate(provs)]

        _ler_com(fontes, timeout=2.0)

        total = sum(p.chamadas for p in provs)
        assert total < 40 * n_fontes, (
            f"laço girando livre: {total} chamadas com {n_fontes} fonte(s) "
            f"em 2s de orçamento")

    def test_duas_fontes_nao_sao_pior_que_uma(self, ambiente, visao_falsa, pipeline_vivo):
        """O sintoma exato: uma câmera se freava, duas não.

        Duas fontes fazem ~2x as chamadas de uma (cada uma tem a sua cadência), e é isso
        que se exige — não que o total seja igual. O bug dava um fator de ~40.000.
        """
        visao_falsa(_DetectorFalso([]), _OcrFalso())

        p1 = _ProviderMudo()
        _ler_com([_fonte(21, p1)], timeout=2.0)

        p2a, p2b = _ProviderMudo(), _ProviderMudo()
        _ler_com([_fonte(22, p2a), _fonte(23, p2b)], timeout=2.0)

        uma = p1.chamadas
        duas = p2a.chamadas + p2b.chamadas
        assert duas < uma * 5, (
            f"duas fontes explodiram: {duas} chamadas contra {uma} de uma só")


class TestOCaminhoBomNaoMudou:
    """A metade que impede "consertar" introduzindo espera onde não havia."""

    def test_camera_publicando_tira_todas_as_fotos(self, ambiente, visao_falsa,
                                                   pipeline_vivo):
        """Com frame novo a cada chamada, o laço gasta o orçamento de TENTATIVAS.

        Se a cadência tivesse passado a frear o caminho saudável, o número de fotos cairia
        abaixo do teto dentro do mesmo orçamento de tempo.
        """
        visao_falsa(_DetectorFalso([]), _OcrFalso())

        def frames():
            # Objeto NOVO a cada chamada: o laço compara por identidade para não votar
            # duas vezes no mesmo frame.
            return np.zeros((480, 640, 3), dtype=np.uint8)

        fontes = [_fonte(cam, frames) for cam in (31, 32)]
        _ler_com(fontes, timeout=30.0)

        n_max = int(CFG["leitura_max_tentativas"])
        assert sum(f.tentativas for f in fontes) == n_max
        # E cada uma contribuiu: o revezamento continua alternando as fontes.
        assert all(f.tentativas > 0 for f in fontes)

    def test_primeira_foto_de_cada_fonte_nao_espera(self, ambiente, visao_falsa,
                                                    pipeline_vivo):
        """`ultimo_ts` começa em 0.0, então a primeira visita salta a espera.

        Prende a diferença entre "guardar em `ultimo_ts`" e "dormir sempre": a primeira
        foto de cada fonte tem de sair na hora, senão a correção custaria latência ao
        roteador em TODA leitura. Com 2 fontes e orçamento de 0,10s (menor que o intervalo
        de 0,15s), só dá tempo das duas primeiras fotos — e as duas têm de sair.
        """
        visao_falsa(_DetectorFalso([]), _OcrFalso())
        fontes = [_fonte(cam, lambda: np.zeros((480, 640, 3), dtype=np.uint8))
                  for cam in (41, 42)]

        _ler_com(fontes, timeout=0.10)

        assert all(f.tentativas >= 1 for f in fontes), (
            f"alguma fonte esperou à toa na 1a foto: "
            f"{[f.tentativas for f in fontes]}")
