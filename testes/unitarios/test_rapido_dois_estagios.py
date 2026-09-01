"""`rapido_dois_estagios` desacopla o perfil rapido do pipeline continuo.

Os dois consumiam a MESMA chave (`veiculo_dois_estagios_live`) e tem necessidades opostas:
o continuo roda em todo quadro das duas cameras e e diagnostico; o rapido roda 1-2x por
abastecimento e e o que o roteador le.

Medido em 01/09/2026: com 2 estagios o detector do rapido faz 44/54 no dataset, sem faz
35/54 (nove placas). E desligar o 2 estagios do CONTINUO leva a chamada rapida de
5,2-11,1 s para 2,1-2,3 s, porque ele para de disputar CPU. Com uma chave so, as duas
medicoes se anulam -- ou rapido e certeiro e lento, ou veloz e ruim.
"""
from __future__ import annotations

import pytest

from app.visao import detector as det_mod


def _resolve(cfg: dict) -> bool:
    """A mesma expressao de `obter_detector_rapido`, isolada para o teste.

    Testa a REGRA, nao o detector: construir o detector de verdade carrega ONNX e, em
    paralelo ao resto da suite no Windows, e a receita do `Unknown C++ exception from
    OpenCV code` que ja custou uma medicao inteira neste projeto.
    """
    override = str(cfg.get("rapido_dois_estagios", "")).strip()
    if override:
        return det_mod._bool_cfg({"x": override}, "x")
    return det_mod._bool_cfg(cfg, "veiculo_dois_estagios_live")


class TestResolucaoDaChave:

    @pytest.mark.parametrize("live,esperado", [("sim", True), ("nao", False)])
    def test_vazio_segue_o_continuo(self, live, esperado):
        """Default `""` = comportamento historico, sem mudanca para quem nao configurou."""
        assert _resolve({"veiculo_dois_estagios_live": live}) is esperado

    @pytest.mark.parametrize("override,esperado", [("sim", True), ("nao", False)])
    def test_override_vence_o_continuo(self, override, esperado):
        """O caso que motivou a chave: continuo leve + rapido certeiro."""
        for live in ("sim", "nao"):
            assert _resolve({"veiculo_dois_estagios_live": live,
                             "rapido_dois_estagios": override}) is esperado

    def test_a_combinacao_alvo(self):
        """Continuo desligado (libera CPU), rapido ligado (mantem as 9 placas)."""
        assert _resolve({"veiculo_dois_estagios_live": "nao",
                         "rapido_dois_estagios": "sim"}) is True

    def test_espaco_em_branco_conta_como_vazio(self):
        assert _resolve({"veiculo_dois_estagios_live": "sim",
                         "rapido_dois_estagios": "   "}) is True


class TestChaveNoPadroes:

    def test_esta_em_PADROES(self):
        """`web/api.py` rejeita com 400 qualquer chave fora de `CHAVES_CONFIG`."""
        from app.core import config
        assert config.PADROES.get("rapido_dois_estagios") == ""

    def test_o_ident_do_cache_cobre_a_chave(self):
        """Ident incompleto = ajuste salvo, confirmado na tela, e que nunca chega ao
        detector cacheado ate o processo reiniciar. Ja custou caro neste projeto."""
        import inspect
        src = inspect.getsource(det_mod.obter_detector_rapido)
        assert "rapido_dois_estagios" in src
        assert "dois_estagios," in src, "o valor resolvido tem de entrar no ident"
