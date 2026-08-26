"""Consulta de veículo sob demanda: o caminho em que gastar é decisão de um humano.

Com cota curta, o abastecimento não consulta sozinho (`apiplacas_modo=manual`) e estas
rotas são o jeito deliberado de gastar. Duas coisas importam aqui:

1. a rota que gasta gasta MESMO em modo manual — senão não haveria como usar a cota;
2. as outras duas NUNCA gastam, porque são chamadas em toda navegação do painel.

Como no resto da suíte, os casos de custo afirmam sobre o contador de chamadas à fronteira
`buscar_na_api`, e não reexecutam a regra que decide gastar.
"""
from __future__ import annotations

import pytest

from app.core import banco, config
from app.integracoes import apiplacas
from app.seguranca import limitador

PLACA = "ABC1D23"
DOC = {"marca": "VW", "modelo": "CROSSFOX", "ano": "2007", "cor": "Prata",
       "extra": {"combustivel": "Alcool / Gasolina", "especie": "Passageiro"},
       "fipe": {"dados": [{"score": 101, "sigla_combustivel": "G"}]}}


@pytest.fixture
def cenario(ambiente, admin, monkeypatch):
    """Recurso ligado em modo MANUAL (o padrão) e a fronteira HTTP contada."""
    config.salvar({**config.carregar(), "apiplacas_ativo": "sim",
                   "apiplacas_modo": "manual", "apiplacas_token": "TOKEN-SECRETO"})
    limitador._resetar_para_teste()
    apiplacas.limpar_pausa()

    chamadas: list[str] = []
    resposta = {"valor": (200, DOC, "")}

    def _fronteira(placa, token, timeout_seg, base_url):
        chamadas.append(placa)
        return resposta["valor"]

    monkeypatch.setattr(apiplacas, "buscar_na_api", _fronteira)
    yield type("C", (), {"admin": admin, "chamadas": chamadas,
                         "responder": staticmethod(lambda v: resposta.update(valor=v))})()
    apiplacas.limpar_pausa()


class TestConsultaManual:
    def test_gasta_mesmo_em_modo_manual(self, cenario):
        """O ponto da feature: `manual` impede o gasto AUTOMÁTICO, não o deliberado."""
        r = cenario.admin.post(f"/api/veiculos/{PLACA}/consultar")
        assert r.status_code == 200, r.text
        v = r.json()["veiculo"]
        assert v["consulta"] == "ok" and v["origem"] == "api"
        assert v["combustivel"] == "Alcool / Gasolina"
        assert cenario.chamadas == [PLACA]

    def test_grava_e_a_leitura_seguinte_nao_gasta(self, cenario, posto, monkeypatch):
        """Fecha o ciclo: consultei uma vez à mão, o abastecimento passa a ter o dado."""
        cenario.admin.post(f"/api/veiculos/{PLACA}/consultar")

        from app.web import leitura as leitura_rotas
        monkeypatch.setattr(leitura_rotas.leitura, "ler_placa", lambda **kw: {
            "camera_id": 1, "bico_id": 1, "placa": PLACA, "padrao": "mercosul",
            "confianca": 0.9, "votos_snapshot": 3, "total_snapshots": 3, "votos_ocr": 1,
            "total_engines": 1, "detalhes_ocr": [], "snapshot": None, "frame_url": None,
            "tentativas": 3, "acordo": 0.9, "confirmada": True, "parada_motivo": "acordo",
            "tipo_veiculo": "carro", "n_cameras_votando": 1, "fontes": [], "avisos": [],
        })
        limitador._resetar_para_teste()
        corpo = cenario.admin.get("/api/leitura", params={
            "entidade": "Rede Teste", "cnpj": posto["cnpj"], "automacao": "1", "bico": "3",
        }).json()
        assert corpo["veiculo"]["origem"] == "cache"
        assert corpo["veiculo"]["combustivel"] == "Alcool / Gasolina"
        assert len(cenario.chamadas) == 1, "o abastecimento não pode ter pago de novo"

    def test_normaliza_a_placa(self, cenario):
        """Minúscula pela URL não pode virar uma segunda linha (e uma segunda cobrança)."""
        cenario.admin.post(f"/api/veiculos/{PLACA.lower()}/consultar")
        cenario.admin.post(f"/api/veiculos/{PLACA}/consultar")
        assert len(cenario.chamadas) == 1
        assert banco.veiculos_stats()["total"] == 1

    def test_placa_invalida_nao_gasta(self, cenario):
        r = cenario.admin.post("/api/veiculos/ABC/consultar")
        assert r.status_code == 400
        assert cenario.chamadas == []

    def test_sem_token_nao_gasta(self, ambiente, admin, monkeypatch):
        chamadas = []
        monkeypatch.setattr(apiplacas, "buscar_na_api",
                            lambda *a, **k: chamadas.append(1) or (200, DOC, ""))
        config.salvar({**config.carregar(), "apiplacas_ativo": "sim", "apiplacas_token": ""})
        r = admin.post(f"/api/veiculos/{PLACA}/consultar")
        assert r.status_code == 400
        assert chamadas == []

    def test_clique_duplo_nao_vira_dois_creditos(self, cenario):
        """O cooldown por placa existe para isto. Na segunda vez o cache já responde."""
        cenario.admin.post(f"/api/veiculos/{PLACA}/consultar")
        r2 = cenario.admin.post(f"/api/veiculos/{PLACA}/consultar")
        assert len(cenario.chamadas) == 1
        assert r2.json()["veiculo"]["origem"] == "cache"

    def test_e_auditado_sem_vazar_o_token(self, cenario):
        """Ação que custa dinheiro precisa ter dono."""
        cenario.admin.post(f"/api/veiculos/{PLACA}/consultar")
        regs = banco.auditoria_listar(limit=20)
        meu = [r for r in regs if r["acao"] == "veiculo_consultado"]
        assert meu, "a consulta paga tem de ficar auditada"
        assert meu[0]["alvo_id"] == PLACA
        assert "TOKEN-SECRETO" not in str(regs)

    def test_cache_hit_nao_polui_a_auditoria(self, cenario):
        """Clique que caiu no cache não gastou nada — não vira linha permanente."""
        cenario.admin.post(f"/api/veiculos/{PLACA}/consultar")
        cenario.admin.post(f"/api/veiculos/{PLACA}/consultar")
        pagas = [r for r in banco.auditoria_listar(limit=20) if r["acao"] == "veiculo_consultado"]
        assert len(pagas) == 1

    def test_so_admin_gasta(self, cenario, cliente_logado):
        assert cliente_logado.post(f"/api/veiculos/{PLACA}/consultar").status_code == 403
        assert cenario.chamadas == []


class TestCacheOnly:
    def test_get_veiculos_nunca_consulta(self, cenario, monkeypatch):
        """Esta rota é chamada em toda navegação do histórico: um gasto aqui seria
        proporcional ao uso do painel."""
        def _explodir(*a, **k):
            raise AssertionError("GET /api/veiculos não pode chamar a API paga")
        monkeypatch.setattr(apiplacas, "buscar_na_api", _explodir)
        r = cenario.admin.get("/api/veiculos", params={"placas": f"{PLACA},XYZ9W88"})
        assert r.status_code == 200
        assert r.json() == {PLACA: None, "XYZ9W88": None}

    def test_get_veiculos_devolve_o_que_esta_em_cache(self, cenario):
        cenario.admin.post(f"/api/veiculos/{PLACA}/consultar")
        corpo = cenario.admin.get("/api/veiculos", params={"placas": f"{PLACA},XYZ9W88"}).json()
        assert corpo[PLACA]["combustivel"] == "Alcool / Gasolina"
        assert corpo["XYZ9W88"] is None, "placa sem dados tem de vir explicitamente nula"

    def test_negativa_aparece_como_consultada(self, cenario):
        """"Consultada e não existe" é diferente de "nunca consultada": só a segunda deve
        oferecer o botão de gastar."""
        cenario.responder((406, {"message": "Sem resultados"}, ""))
        cenario.admin.post(f"/api/veiculos/{PLACA}/consultar")
        corpo = cenario.admin.get("/api/veiculos", params={"placas": PLACA}).json()
        assert corpo[PLACA] is not None
        assert corpo[PLACA]["consulta"] == "inexistente"

    def test_operador_ve_o_cache(self, cenario, operador_logado):
        """Ver é operar; gastar é administrar."""
        cenario.admin.post(f"/api/veiculos/{PLACA}/consultar")
        r = operador_logado.get("/api/veiculos", params={"placas": PLACA})
        assert r.status_code == 200
        assert r.json()[PLACA]["combustivel"] == "Alcool / Gasolina"

    def test_desligado_devolve_vazio(self, cenario):
        config.salvar({**config.carregar(), "apiplacas_ativo": "nao"})
        assert cenario.admin.get("/api/veiculos", params={"placas": PLACA}).json() == {}


class TestPendentes:
    def _ler(self, placa, origem="roteador", vezes=1):
        for _ in range(vezes):
            banco.registrar_deteccao(placa, "mercosul", 0.9, origem=origem)

    def test_ordena_por_frequencia(self, cenario):
        """Com cota curta, o crédito rende mais na frota que volta toda semana."""
        self._ler("AAA1A11", vezes=2)
        self._ler("BBB2B22", vezes=5)
        self._ler("CCC3C33", vezes=3)
        placas = [p["placa"] for p in cenario.admin.get("/api/veiculos/pendentes").json()["placas"]]
        assert placas == ["BBB2B22", "CCC3C33", "AAA1A11"]

    def test_exclui_quem_ja_tem_dados(self, cenario):
        self._ler("AAA1A11", vezes=3)
        self._ler(PLACA, vezes=9)
        cenario.admin.post(f"/api/veiculos/{PLACA}/consultar")
        placas = [p["placa"] for p in cenario.admin.get("/api/veiculos/pendentes").json()["placas"]]
        assert placas == ["AAA1A11"], "a placa já paga não pode voltar para a lista"

    def test_negativa_tambem_sai_da_lista(self, cenario):
        """406 é resposta definitiva sobre aquele veículo — repropor seria repagar por
        uma informação que já temos."""
        self._ler(PLACA, vezes=4)
        cenario.responder((406, {"message": "Sem resultados"}, ""))
        cenario.admin.post(f"/api/veiculos/{PLACA}/consultar")
        assert cenario.admin.get("/api/veiculos/pendentes").json()["placas"] == []

    def test_cache_vencido_nao_e_pendente(self, cenario):
        """Vencido ainda entrega dado bom; o lote não deve gastar em quem já está
        atendido."""
        self._ler(PLACA, vezes=4)
        banco.veiculos_salvar(PLACA, status="ok", campos={"combustivel": "Diesel"})
        with banco.cursor() as c:
            c.execute("UPDATE veiculos SET consultado_em = ? WHERE placa = ?",
                      ("2015-01-01T00:00:00+00:00", PLACA))
        assert cenario.admin.get("/api/veiculos/pendentes").json()["placas"] == []

    def test_ignora_leitura_de_teste(self, cenario):
        """Ajustar enquadramento não é movimento do posto e não deve dirigir o gasto."""
        self._ler("AAA1A11", origem="teste", vezes=9)
        self._ler("BBB2B22", vezes=1)
        placas = [p["placa"] for p in cenario.admin.get("/api/veiculos/pendentes").json()["placas"]]
        assert placas == ["BBB2B22"]

    def test_informa_o_custo_antes_de_gastar(self, cenario):
        """Pedir confirmação sem dizer o preço não é confirmação."""
        self._ler("AAA1A11", vezes=2)
        self._ler("BBB2B22", vezes=1)
        corpo = cenario.admin.get("/api/veiculos/pendentes").json()
        assert len(corpo["placas"]) == 2
        assert corpo["custo_consulta"] == pytest.approx(0.03)
        assert corpo["custo_total"] == pytest.approx(0.06)

    def test_respeita_o_limit(self, cenario):
        for p in ["AAA1A11", "BBB2B22", "CCC3C33"]:
            self._ler(p)
        assert len(cenario.admin.get("/api/veiculos/pendentes",
                                     params={"limit": 2}).json()["placas"]) == 2

    def test_pendentes_e_so_admin(self, cenario, cliente_logado):
        assert cliente_logado.get("/api/veiculos/pendentes").status_code == 403

    def test_escopo_por_posto_filtra(self, ambiente, posto):
        """`veiculos_pendentes` aceita `empresa_id` e a rota passa o escopo do usuário.

        Hoje a rota é admin-only e admin não tem escopo, então este ramo do SQL não é
        exercitado por nenhuma requisição — mas ele é o que impediria um posto de propor
        (e pagar) consulta de placa que nem pode ver, no dia em que a rota abrir para
        `cliente`. Testado direto na função para não ficar um ramo sem cobertura nenhuma.
        """
        banco.registrar_deteccao("AAA1A11", "mercosul", 0.9, bico_id=posto["bico_id"])
        banco.registrar_deteccao("BBB2B22", "mercosul", 0.9)   # sem bico: outro posto

        do_posto = banco.veiculos_pendentes(empresa_id=posto["empresa_id"])
        assert [p["placa"] for p in do_posto] == ["AAA1A11"]

        sem_escopo = banco.veiculos_pendentes()
        assert {p["placa"] for p in sem_escopo} == {"AAA1A11", "BBB2B22"}
