"""Perfil de captura do botão "Forçar leitura" da vitrine (`feira_forcar_perfil`).

O que estes testes protegem:

1. O PERFIL CHEGA em `ler_placa`. A asserção é sobre o argumento que a rota passa adiante,
   não sobre o retorno de `perfil_forcar_feira` — verificar o resolver contra a mesma regra
   que ele implementa seria reimplementá-la no teste, e este projeto já teve 27 testes
   verdes com a feature quebrada exatamente assim.
2. O LOOP não muda. `forcar=0` usa o rápido em qualquer configuração: é ele que varre a
   cada ~1,6s sozinho, e deixá-lo cair no completo poria a máquina a gastar 28s de CPU por
   varredura sem ninguém ter pedido.
3. Config ilegível cai no COMPLETO. O default é o perfil mais robusto, e o pior caso de um
   typo tem de ser o botão demorar o que sempre demorou — nunca a demo ficar menos capaz do
   que o operador pensa.
"""
from __future__ import annotations

import pytest

from app.core import config
from app.seguranca import limitador
from app.visao import leitura as leitura_mod
from app.web import cadastro as cadastro_rotas

DEMO = "MOK3H92"


@pytest.fixture
def vitrine(ambiente, admin, posto, monkeypatch):
    """Posto de demonstração armado, com o `perfil` recebido por `ler_placa` capturado."""
    config.salvar({**config.carregar(),
                   "feira_ativo": "sim", "feira_placas": DEMO,
                   "feira_empresa_id": str(posto["empresa_id"])})
    limitador._resetar_para_teste()

    recebidos: list[str] = []

    def _ler_placa(**kw):
        recebidos.append(kw["perfil"])
        return {"placa": DEMO, "confianca": 0.9, "mockada": True, "confirmada": True,
                "tipo_veiculo": "carro", "avisos": [], "fontes": []}

    monkeypatch.setattr(cadastro_rotas.leitura, "ler_placa", _ler_placa)

    def _scan(forcar: bool = False):
        # A rota tem teto por IP; vários scans na mesma classe estourariam o limite e
        # receberiam 429 em vez do que está sendo medido.
        limitador._resetar_para_teste()
        return admin.post("/api/feira/scan", params={"forcar": 1} if forcar else {})

    return type("V", (), {"scan": staticmethod(_scan), "perfis": recebidos})()


def _com(valor: str | None):
    cfg = dict(config.carregar())
    if valor is None:
        cfg.pop("feira_forcar_perfil", None)
    else:
        cfg["feira_forcar_perfil"] = valor
    config.salvar(cfg)


class TestBotaoForcar:
    def test_default_e_completo(self, vitrine):
        """Sem configurar nada, o botão segue com o perfil robusto de sempre."""
        assert config.PADROES["feira_forcar_perfil"] == "completo"
        vitrine.scan(forcar=True)
        assert vitrine.perfis == [leitura_mod.PERFIL_COMPLETO]

    def test_configurado_rapido_usa_rapido(self, vitrine):
        """O requisito inteiro: o operador escolhe rápido e o botão responde rápido."""
        _com("rapido")
        vitrine.scan(forcar=True)
        assert vitrine.perfis == [leitura_mod.PERFIL_RAPIDO]

    def test_configurado_completo_usa_completo(self, vitrine):
        _com("completo")
        vitrine.scan(forcar=True)
        assert vitrine.perfis == [leitura_mod.PERFIL_COMPLETO]

    @pytest.mark.parametrize("valor", ["", "  ", "xyz", "completa", "Rápido", None])
    def test_valor_ilegivel_cai_no_completo(self, vitrine, valor):
        """Fail-safe na direção certa — ver o cabeçalho deste arquivo.

        "Rápido" com acento entra aqui de propósito: quem digita à mão escreve assim, e o
        valor aceito é `rapido`. Cair no completo é o desfecho certo (demora, não degrada).
        """
        _com(valor)
        vitrine.scan(forcar=True)
        assert vitrine.perfis == [leitura_mod.PERFIL_COMPLETO]

    def test_maiuscula_e_espaco_sao_aceitos(self, vitrine):
        """`config.txt` é editável à mão; " RAPIDO " é a mesma intenção."""
        _com("  RAPIDO  ")
        vitrine.scan(forcar=True)
        assert vitrine.perfis == [leitura_mod.PERFIL_RAPIDO]


class TestLoopNaoMuda:
    """O loop hands-free usa o rápido SEMPRE — a config é só do botão."""

    @pytest.mark.parametrize("valor", ["completo", "rapido", "xyz"])
    def test_loop_sempre_rapido(self, vitrine, valor):
        _com(valor)
        vitrine.scan(forcar=False)
        assert vitrine.perfis == [leitura_mod.PERFIL_RAPIDO], (
            "o loop varre sozinho a cada ~1,6s: cair no completo gastaria 28s de CPU "
            "por varredura sem ninguém ter pedido"
        )
