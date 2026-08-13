"""Corte de recorte degenerado: o que não tem pixel não vai a engine nenhum.

MEDIDO em 13/08/2026 sobre as capturas reais em `app/web/static/snapshots/` — 936
recortes não-lidos e 526 que produziram leitura "válida". O corte em 24x10 barra 38,5%
dos não-lidos e custa 4 das 526 leituras, todas com no máximo 2,4 px por caractere
(3x2 px, 11x11, 13x6, 17x8): alucinação que estava sendo GRAVADA como detecção.

O que estes testes protegem, e que é fácil desfazer sem perceber:

  1. O corte é medido no recorte CRU, antes de `_realcar_para_ocr`. Ampliar não cria
     informação — se a checagem migrar para depois do realce, ela para de barrar
     qualquer coisa, porque o realce leva tudo para 224 px de altura.
  2. Recorte barrado não roda engine. É o ponto todo: eram ~600 ms por recorte
     impossível, e uma leitura inventada entrando como voto no tracker.
  3. Recorte barrado não vai para a fila de classificação. Ninguém consegue rotular
     uma imagem de 3x1 px, e ela consome a cota de `captura_dataset_max_arquivos`
     que existe para guardar o caso difícil.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.visao.ocr.auto import (CROP_MIN_ALTURA, CROP_MIN_LARGURA, AutoOCR,
                                crop_legivel)
from app.visao.pipeline import _vale_como_negativo


def _img(w: int, h: int) -> np.ndarray:
    return np.full((h, w, 3), 127, dtype=np.uint8)


class _EngineQueExplode:
    """Engine que falha o teste se for chamado — o corte tem que acontecer antes."""

    engine = "nao-devia-rodar"

    def ler(self, crop):
        raise AssertionError("engine rodou num recorte que devia ter sido descartado")

    def _remover_header(self, crop):
        raise AssertionError("pré-processamento rodou num recorte descartado")


def _auto_sem_engines() -> AutoOCR:
    """AutoOCR sem `__init__` — igual ao padrão de test_consenso_pipeline.py, que evita
    construir OCR de verdade. Os dois engines explodem se alguém os chamar."""
    o = AutoOCR.__new__(AutoOCR)
    o._fast = _EngineQueExplode()
    o._easy = _EngineQueExplode()
    o.engine = "auto"
    o._ultimo_detalhe = {}
    return o


class TestLimiar:
    def test_limiar_e_o_medido(self):
        """Se alguém mexer nos números, que seja de propósito: eles vêm de uma medição
        em 1462 imagens reais, e afrouxar custa leitura verdadeira (40x12 custaria 4,2%)."""
        assert (CROP_MIN_LARGURA, CROP_MIN_ALTURA) == (24, 10)

    @pytest.mark.parametrize("w,h", [(24, 10), (24, 99), (999, 10), (58, 26)])
    def test_no_limiar_ou_acima_passa(self, w, h):
        assert crop_legivel(w, h)

    @pytest.mark.parametrize("w,h", [(23, 10), (24, 9), (3, 1), (17, 8), (13, 6), (11, 11)])
    def test_abaixo_em_qualquer_dimensao_nao_passa(self, w, h):
        """As quatro últimas são as leituras reais que o corte descarta — todas
        geometricamente impossíveis para uma placa de sete caracteres."""
        assert not crop_legivel(w, h)


class TestNaoRodaEngine:
    @pytest.mark.parametrize("w,h", [(4, 3), (17, 6), (3, 1), (23, 9)])
    def test_recorte_degenerado_nao_chama_engine(self, w, h):
        o = _auto_sem_engines()
        d = o.ler_detalhado(_img(w, h))
        assert d["placa"] is None
        assert d["confianca"] == 0.0
        # total_engines=0 é o que distingue "descartei" de "rodei os dois e nenhum
        # validou" (que devolve 2) para quem lê `_ultimo_detalhe`.
        assert d["total_engines"] == 0
        assert d["detalhes"] == []

    def test_crop_invalido_tambem_sai_antes(self):
        o = _auto_sem_engines()
        for entrada in (None, np.zeros((0, 0, 3), dtype=np.uint8), np.zeros((5, 5), dtype=np.uint8)):
            assert o.ler_detalhado(entrada)["total_engines"] == 0

    def test_ler_mantem_o_contrato_de_tupla(self):
        """`ler()` promete (str, float) a quem chama — pipeline e leitura reativa fazem
        `validar(texto)` direto no retorno, e um None ali quebraria os dois."""
        texto, conf = _auto_sem_engines().ler(_img(4, 3))
        assert texto == ""
        assert conf == 0.0

    def test_corte_vale_sobre_o_recorte_cru(self):
        """Um 12x5 CINZA seria ampliado por `_realcar_para_ocr` (lapvar 0 < 3500) para
        224 px de altura. Se a checagem rodasse depois do realce, passaria — e o engine
        receberia uma interpolação de 60 pixels de informação."""
        o = _auto_sem_engines()
        assert o.ler_detalhado(_img(12, 5))["total_engines"] == 0


class TestFilaDeClassificacao:
    @pytest.mark.parametrize("w,h", [(3, 1), (17, 6), (23, 9)])
    def test_degenerado_nao_vira_imagem_para_rotular(self, w, h):
        assert not _vale_como_negativo(_img(w, h))

    @pytest.mark.parametrize("w,h", [(24, 10), (58, 26), (300, 100)])
    def test_recorte_util_continua_indo(self, w, h):
        """O negativo é a única fonte do caso difícil — o histórico só guarda acerto.
        Barrar demais aqui secaria justamente o dado que falta."""
        assert _vale_como_negativo(_img(w, h))

    def test_crop_invalido_nao_vira_arquivo(self):
        assert not _vale_como_negativo(None)
        assert not _vale_como_negativo(np.zeros((0, 0, 3), dtype=np.uint8))
