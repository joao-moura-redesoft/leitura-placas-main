"""Fichas da vitrine da feira (app/visao/feira_fichas.py) e a rota /feira.

O que estes testes protegem:

1. A vitrine NUNCA aparece vazia: o carrinho canônico (MOK3H92) tem ficha embutida mesmo
   sem ninguém cadastrar nada.
2. Placa é a mesma para humano e para o código: MOK-3H92, "mok 3h92" e MOK3H92 casam a
   mesma ficha (normalização), como no resto do modo feira.
3. A ficha some quando removida: o PUT grava o conjunto inteiro (sem merge), então
   remover uma linha e salvar apaga de verdade.
4. A aba /feira só existe com o modo armado — senão redireciona, e o link nem aparece.
5. A ficha alimenta o bloco `veiculo` do payload OFFLINE com a forma e os TIPOS da
   consulta real — ver `TestBlocoVeiculo`.
"""
from __future__ import annotations

from app.core import banco, config
from app.integracoes import apiplacas
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


class TestBlocoVeiculo:
    """Ficha → bloco `veiculo` do payload, que é como a demo entrega dados sem internet.

    O consumidor é o sidecar Java tipado do posto (docs/INTEGRACAO_ROTEADOR.md). O que
    importa aqui não é "veio algo", é veio com a MESMA forma e os MESMOS tipos da consulta
    real — divergir num tipo entrega um bloco que passa em teste de chaves e quebra no
    parse do outro lado.
    """

    def _leitura(self, placa="MOK3H92", mockada=True):
        return {"mockada": mockada, "placa": placa}

    def test_leitura_real_nao_ganha_bloco_de_demo(self, ambiente):
        """O desfecho padrão: leitura não mockada segue para a apiplacas de sempre."""
        assert feira_fichas.bloco_de_leitura(self._leitura(mockada=False)) is None

    def test_origem_feira_sozinha_nao_basta(self, ambiente):
        """A vitrine pede `origem="feira"` em TODA leitura sua, mockada ou não.

        Se o gancho olhasse a origem, a placa do celular de um visitante escaneada pelo
        kiosk receberia a ficha do carrinho — exatamente o que o modo feira promete não
        fazer (ver `test_placa_de_visitante_passa_intacta`).
        """
        assert feira_fichas.bloco_de_leitura(
            {"origem": "feira", "mockada": False, "placa": "ABC1D23"}) is None

    def test_sem_placa_nao_monta_bloco(self, ambiente):
        assert feira_fichas.bloco_de_leitura({"mockada": True, "placa": None}) is None

    def test_forma_identica_a_da_consulta_real(self, ambiente):
        """Mesmas chaves de `apiplacas.CHAVES_VEICULO`, nem uma a mais nem a menos."""
        bloco = feira_fichas.bloco_de_leitura(self._leitura())
        assert set(bloco) == set(apiplacas.CHAVES_VEICULO)

    def test_dados_da_ficha_chegam_ao_bloco(self, ambiente):
        bloco = feira_fichas.bloco_de_leitura(self._leitura())
        assert bloco["consulta"] == apiplacas.CONSULTA_OK
        assert bloco["combustivel"] == "Flex"
        assert bloco["marca"] == "VW"
        assert bloco["cor"] == "Cinza"
        assert bloco["situacao"] == "Sem restrição"

    def test_ano_sai_como_numero_nao_string(self, ambiente):
        """A ficha guarda "2024"; o bloco real entrega 2024. O tipo faz parte do contrato."""
        bloco = feira_fichas.bloco_de_leitura(self._leitura())
        assert bloco["ano"] == 2024 and isinstance(bloco["ano"], int)
        assert bloco["ano_modelo"] == 2024 and isinstance(bloco["ano_modelo"], int)

    def test_campo_em_branco_sai_nulo_nao_string_vazia(self, ambiente):
        """`""` faria o consumidor tratar "não informado" como valor."""
        bloco = feira_fichas.bloco_de_leitura(self._leitura())
        assert bloco["municipio"] is None
        assert bloco["uf"] is None
        assert bloco["combustivel_sigla"] is None

    def test_ano_nao_numerico_sai_nulo_sem_quebrar(self, ambiente):
        """A ficha é texto livre editado à mão — "2024/2025" não pode derrubar a demo."""
        feira_fichas.salvar_fichas({"MOK3H92": {"modelo": "X", "ano": "2024/2025"}})
        bloco = feira_fichas.bloco_de_leitura(self._leitura())
        assert bloco["ano"] is None
        assert bloco["modelo"] == "X"

    def test_declara_que_e_demonstracao(self, ambiente):
        """Sem isto o bloco sintético ficaria indistinguível de uma consulta paga."""
        bloco = feira_fichas.bloco_de_leitura(self._leitura())
        assert bloco["origem"] == apiplacas.ORIGEM_DEMONSTRACAO == "feira"
        assert "demonstracao" in bloco["motivo"].lower()

    def test_placa_mockada_sem_ficha_diz_o_que_falta(self, ambiente):
        """Bloco `indisponivel` com motivo, e não `ok` com tudo nulo nem ausência de bloco:
        "esqueci de preencher a ficha" tem de aparecer antes de alguém descobrir na feira.
        """
        bloco = feira_fichas.bloco_de_leitura(self._leitura(placa="ZZZ9Z99"))
        assert bloco["consulta"] == apiplacas.CONSULTA_INDISPONIVEL
        assert "ficha" in bloco["motivo"].lower()
        assert set(bloco) == set(apiplacas.CHAVES_VEICULO)

    def test_a_ficha_cobre_todas_as_chaves_curadas(self):
        """O guarda que faz campo NOVO da apiplacas aparecer como campo faltando aqui.

        Sem ele, uma chave nova em `banco.CAMPOS_CURADOS` sairia `null` na demo para
        sempre, sem nenhum sintoma — e ninguém descobre num payload cujos campos nulos
        são desfecho legítimo.
        """
        assert set(banco.CAMPOS_CURADOS) <= set(feira_fichas.CAMPOS)

    def test_campos_so_do_kiosk_nao_vazam_para_o_payload(self, ambiente):
        """`apelido`/`mensagem` são texto de tela, não campo de registro de veículo."""
        bloco = feira_fichas.bloco_de_leitura(self._leitura())
        assert "apelido" not in bloco and "mensagem" not in bloco

    def test_nao_grava_no_cache_real_de_veiculos(self, ambiente):
        """Dado sintético no cache da apiplacas contaminaria consulta de posto de verdade
        — e ficaria lá pelos próximos 180 dias (o TTL), muito depois da feira acabar.
        """
        feira_fichas.bloco_de_leitura(self._leitura())
        assert banco.veiculos_obter("MOK3H92") is None
