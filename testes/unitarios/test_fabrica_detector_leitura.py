"""A fábrica do detector de leitura (GET / botão "Ler Placa").

Ela é um SINGLETON cacheado e nada mais a invalida quando a config é salva, então a chave
de cache é o que decide se um ajuste chega ou não ao caminho que o bico aciona. E ela
compõe três camadas (janelas → 2 estágios → placa), onde uma montagem errada é silenciosa:
o detector responde, só responde diferente do que a config pediu.
"""
from __future__ import annotations

import pytest

from app.visao import detector as det_mod
from app.visao.detector import (BuscaEmTiles, DetectorDoisEstagios, OpenImageDetector,
                                obter_detector_leitura)

BASE = {
    "detector_backend": "open_image_models",
    "oim_modelo": "m-pequeno",
    "oim_modelo_leitura": "m-grande",
    "conf_threshold": "0.30",
    "veiculo_dois_estagios_get": "sim",
    "tiles_fallback_get": "sim",
    "veiculo_conf": "0.25",
    "veiculo_max_veiculos": "5",
}


# Quantas vezes um modelo de placa foi de fato CONSTRUÍDO nesta rodada de teste.
_construcoes: list = []


@pytest.fixture(autouse=True)
def _sem_cache_nem_modelo(monkeypatch):
    """Zera o singleton e troca o que `carregar()` CONSTRÓI — não o próprio `carregar()`.

    Stubar `carregar()` apagaria justamente o guarda de idempotência que
    `TestCarregarEIdempotente` precisa exercitar; o teste passaria medindo a si mesmo.
    Aqui o método real sempre roda, só não carrega nada pesado.
    """
    import sys
    import types

    _construcoes.clear()

    class _LicensePlateDetectorFalso:
        def __init__(self, **kw):
            _construcoes.append(kw.get("detection_model") or "?")

    mod = types.ModuleType("open_image_models")
    mod.LicensePlateDetector = _LicensePlateDetectorFalso
    monkeypatch.setitem(sys.modules, "open_image_models", mod)

    monkeypatch.setattr(det_mod, "_detector_leitura", None, raising=False)
    monkeypatch.setattr(det_mod, "_detector_leitura_id", None, raising=False)
    yield


class TestChaveDeCache:
    """Cada chave que a fábrica CONGELA na construção tem de entrar na identidade — senão
    salvar a config muda o stream ao vivo (recriado por `pipeline.reiniciar`) e NÃO muda o
    "Ler Placa", e o diagnóstico inverte: o botão parece ignorar o ajuste."""

    @pytest.mark.parametrize("chave,novo", [
        ("conf_threshold", "0.15"),
        ("veiculo_conf", "0.40"),
        ("veiculo_nms", "0.7"),
        ("veiculo_classes", "2,3"),
        ("veiculo_padding", "0.20"),
        ("veiculo_obrigatorio", "sim"),
        ("veiculo_max_veiculos", "12"),
        ("tiles_sobreposicao", "0.50"),
        ("tiles_max_janelas", "9"),
        ("tiles_lado_alvo", "400"),
        ("tiles_conf", "0.05"),
    ])
    def test_mudar_a_chave_reconstroi_o_detector(self, chave, novo):
        d1 = obter_detector_leitura(dict(BASE))
        d2 = obter_detector_leitura({**BASE, chave: novo})
        assert d1 is not d2, f"mexer em {chave} não chegou ao detector de leitura"

    def test_config_igual_reusa_a_instancia(self):
        """O cache tem de continuar sendo cache: carregar modelo é caro."""
        assert obter_detector_leitura(dict(BASE)) is obter_detector_leitura(dict(BASE))


class TestMontagemDasCamadas:
    def test_ordem_das_camadas(self):
        """Janelas por FORA do 2 estágios: elas só devem varrer quando o caminho normal
        inteiro (veículo→placa, com fallback no recorte todo) não achou nada."""
        d = obter_detector_leitura(dict(BASE))
        assert isinstance(d, BuscaEmTiles)
        assert isinstance(d.detector, DetectorDoisEstagios)

    def test_backend_onnx_nao_aninha_dois_estagios(self, tmp_path):
        """Regressão: a fábrica chamava `criar_detector(cfg)`, que JÁ aplica
        `veiculo_dois_estagios_live` — o resultado (um 2 estágios) era envolvido em OUTRO,
        e o YOLOX passava a rodar uma vez por veículo além da passada normal."""
        cfg = {**BASE, "detector_backend": "onnx",
               "modelo_path": str(tmp_path / "placa.onnx"),
               "veiculo_dois_estagios_live": "sim"}
        d = obter_detector_leitura(cfg)

        interno = d.detector if isinstance(d, BuscaEmTiles) else d
        assert isinstance(interno, DetectorDoisEstagios)
        assert not isinstance(interno.detector_placa, DetectorDoisEstagios), \
            "2 estágios aninhado: o estágio de veículo rodaria duas vezes"

    def test_janelas_nao_recebem_o_estagio_de_veiculo(self, tmp_path):
        """`detector_tiles` tem de ser SÓ placa. Se receber o 2 estágios, cada janela roda
        o YOLOX — o oposto do que o docstring de `BuscaEmTiles` promete — e o tipo obtido
        ali é descartado depois pelo rótulo `TILES`."""
        cfg = {**BASE, "detector_backend": "onnx",
               "modelo_path": str(tmp_path / "placa.onnx"),
               "veiculo_dois_estagios_live": "sim"}
        d = obter_detector_leitura(cfg)
        assert isinstance(d, BuscaEmTiles)
        assert not isinstance(d.detector_tiles, DetectorDoisEstagios)


class TestCarregarEIdempotente:
    """`BuscaEmTiles.carregar` chamava os dois filhos guardando por IDENTIDADE
    (`detector_tiles is not detector`) — que não vê o caso real, em que `detector_tiles`
    está ANINHADO dentro de `detector`. O modelo carregava duas vezes, e a primeira sessão
    ficava presa para sempre por trás de `.sess`.

    O guarda real é o que está sob teste: a fixture troca o que `carregar()` constrói, não
    o método.
    """

    def test_detector_aninhado_carrega_o_modelo_uma_vez_so(self):
        """Config em que `detector_tiles` É o detector de placa aninhado no 2 estágios —
        acontece sempre que `tiles_conf >= conf_threshold`."""
        d = obter_detector_leitura({**BASE, "tiles_conf": "0.30"})
        assert d.detector_tiles is d.detector.detector_placa, "premissa do teste"

        d.carregar()

        assert len(_construcoes) == 1, (
            f"o modelo de placa foi construído {len(_construcoes)}x — a sessão extra fica "
            "presa para sempre por trás de .sess")

    def test_tiles_com_limiar_proprio_carrega_dois_modelos(self):
        """Quando `tiles_conf` é MAIS permissivo, as janelas ganham sessão própria — aí
        duas construções são o esperado, e não um vazamento."""
        d = obter_detector_leitura({**BASE, "tiles_conf": "0.05"})
        assert d.detector_tiles is not d.detector.detector_placa
        d.carregar()
        assert len(_construcoes) == 2

    def test_chamar_carregar_de_novo_nao_reconstroi(self):
        d = obter_detector_leitura(dict(BASE))
        d.carregar()
        n = len(_construcoes)
        d.carregar()
        assert len(_construcoes) == n, "segunda chamada reconstruiu o modelo"
