"""Fichas da vitrine da feira (app/visao/feira_fichas.py) e a rota /feira.

O que estes testes protegem:

1. A vitrine NUNCA aparece vazia: o carrinho canônico (MOK3H92) tem ficha embutida mesmo
   sem ninguém cadastrar nada.
2. Placa é a mesma para humano e para o código: MOK-3H92, "mok 3h92" e MOK3H92 casam a
   mesma ficha (normalização), como no resto do modo feira.
3. A ficha é só exibição e some quando removida: o PUT grava o conjunto inteiro (sem
   merge), então remover uma linha e salvar apaga de verdade.
4. A aba /feira só existe com o modo armado — senão redireciona, e o link nem aparece.
"""
from __future__ import annotations

from app.core import config
from app.visao import feira_fichas


class TestFichasModulo:
    def test_padrao_tem_o_carrinho(self, ambiente):
        fichas = feira_fichas.carregar_fichas()
        assert "MOK3H92" in fichas
        assert fichas["MOK3H92"]["combustivel"]      # nasce com dado, não vazio

    def test_round_trip_normaliza_e_limpa(self, ambiente):
        feira_fichas.salvar_fichas(
            {"abc-1d23": {"modelo": "Fiat", "combustivel": "Flex", "xpto": "lixo"}})
        fichas = feira_fichas.carregar_fichas()
        assert "ABC1D23" in fichas                    # hífen/minúsculas normalizados
        assert fichas["ABC1D23"]["modelo"] == "Fiat"
        assert "xpto" not in fichas["ABC1D23"]        # só os campos conhecidos sobrevivem
        assert "MOK3H92" in fichas                    # o padrão continua ao lado do gravado

    def test_ficha_de(self, ambiente):
        feira_fichas.salvar_fichas({"MOK3H92": {"modelo": "Nivus"}})
        assert feira_fichas.ficha_de("mok 3h92")["modelo"] == "Nivus"
        assert feira_fichas.ficha_de("ZZZ0A00") is None
        assert feira_fichas.ficha_de("") is None
        assert feira_fichas.ficha_de(None) is None

    def test_salvar_descarta_placa_vazia(self, ambiente):
        gravado = feira_fichas.salvar_fichas(
            {"": {"modelo": "x"}, "MOK3H92": {"modelo": "y"}})
        assert set(gravado) == {"MOK3H92"}

    def test_remocao_persiste(self, ambiente):
        feira_fichas.salvar_fichas({"MOK3H92": {"modelo": "a"}, "ABC1D23": {"modelo": "b"}})
        # Salva de novo sem ABC1D23 — o conjunto inteiro é substituído.
        feira_fichas.salvar_fichas({"MOK3H92": {"modelo": "a"}})
        assert feira_fichas.ficha_de("ABC1D23") is None


class TestRotaFeira:
    def _armar(self, empresa_id):
        cfg = config.carregar()
        cfg.update(feira_ativo="sim", feira_placas="MOK-3H92",
                   feira_empresa_id=str(empresa_id))
        config.salvar(cfg)

    def test_desarmado_redireciona(self, admin):
        r = admin.get("/feira", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/postos"

    def test_armado_renderiza_kiosk(self, admin, posto):
        self._armar(posto["empresa_id"])
        r = admin.get("/feira")
        assert r.status_code == 200
        assert "feira-kiosk" in r.text
        # A câmera do posto foi resolvida e injetada no script do kiosk.
        assert f"const CAMERA_ID = {posto['camera_id']}" in r.text


class TestEndpointsFichas:
    def test_get_traz_padrao(self, admin):
        r = admin.get("/api/feira/fichas")
        assert r.status_code == 200
        assert "MOK3H92" in r.json()["fichas"]

    def test_put_salva_e_persiste(self, admin):
        r = admin.put("/api/feira/fichas",
                      json={"fichas": {"MOK-3H92": {"modelo": "Nivus", "combustivel": "Flex"}}})
        assert r.status_code == 200
        assert r.json()["fichas"]["MOK3H92"]["modelo"] == "Nivus"
        r2 = admin.get("/api/feira/fichas")
        assert r2.json()["fichas"]["MOK3H92"]["modelo"] == "Nivus"

    def test_put_exige_objeto_fichas(self, admin):
        r = admin.put("/api/feira/fichas", json={"nada": 1})
        assert r.status_code == 422
