"""Cadastro multi-tenant: validação de CNPJ e resolução (cnpj, automacao, bico) → câmera.

`resolver_bico` é o coração da leitura reativa: é ele que traduz o que o roteador do
posto envia na URL para a câmera e a área certas. Errar aqui significa ler a placa do
pátio de outro cliente, ou responder erro sem dizer qual nível do cadastro está errado.
"""
from __future__ import annotations

import pytest

from app.core import banco
from app.web.cadastro import _cnpj_valido, _normalizar_cnpj


class TestCnpj:
    @pytest.mark.parametrize("cnpj", ["11222333000181", "11.222.333/0001-81"])
    def test_aceita_cnpj_com_digito_verificador_correto(self, cnpj):
        assert _cnpj_valido(_normalizar_cnpj(cnpj))

    @pytest.mark.parametrize("cnpj", [
        "11222333000182",   # dígito verificador errado
        "12223330001811",   # transposição de dígitos
        "11111111111111",   # todos iguais
        "1122233300018",    # curto demais
        "",
    ])
    def test_recusa_cnpj_invalido(self, cnpj):
        assert not _cnpj_valido(_normalizar_cnpj(cnpj))

    def test_normalizacao_tira_pontuacao(self):
        assert _normalizar_cnpj("11.222.333/0001-81") == "11222333000181"


class TestResolverBico:
    def test_resolve_o_cadastro_completo(self, posto):
        reg, motivo = banco.resolver_bico(posto["cnpj"], "1", "3")
        assert motivo is None
        assert reg["bico_id"] == posto["bico_id"]
        assert reg["camera_id"] == posto["camera_id"]

    def test_tolera_espaco_e_caixa_no_codigo(self, posto):
        """O código é um rótulo opaco vindo de outra integração — ' 1 ' e '1' são a
        mesma automação para qualquer humano."""
        reg, motivo = banco.resolver_bico(posto["cnpj"], " 1 ", " 3 ")
        assert motivo is None
        assert reg["bico_id"] == posto["bico_id"]

    @pytest.mark.parametrize("cnpj, auto, bico, esperado", [
        ("99999999999999", "1", "3", "empresa"),
        (None,             "9", "3", "automacao"),
        (None,             "1", "9", "bico"),
    ])
    def test_aponta_o_nivel_exato_que_falhou(self, posto, cnpj, auto, bico, esperado):
        """O time de campo precisa saber ONDE o cadastro está errado, não só que falhou."""
        _reg, motivo = banco.resolver_bico(cnpj or posto["cnpj"], auto, bico)
        assert motivo == esperado

    @pytest.mark.parametrize("nivel, tabela, campo", [
        ("entidade_inativa",  "entidades",  "entidade_id"),
        ("empresa_inativa",   "empresas",   "empresa_id"),
        ("automacao_inativa", "automacoes", "automacao_id"),
        ("bico_inativo",      "bicos",      "bico_id"),
        ("camera_inativa",    "cameras",    "camera_id"),
    ])
    def test_distingue_desativado_de_inexistente(self, posto, nivel, tabela, campo):
        """Reativar e cadastrar são correções diferentes — a mensagem tem que separar
        as duas. Antes só o `ativo` do bico era olhado: desativar o posto, a automação,
        a câmera ou a entidade não impedia a leitura de continuar respondendo."""
        with banco.cursor() as c:
            c.execute(f"UPDATE {tabela} SET ativo=0 WHERE id=?", (posto[campo],))
        reg, motivo = banco.resolver_bico(posto["cnpj"], "1", "3")
        assert reg is None
        assert motivo == nivel

    def test_mesmo_gate_partindo_do_id_do_bico(self, posto):
        """`bico_verificar_ativo` protege as rotas internas (botão de teste do painel)
        com as mesmas regras da leitura reativa — senão o teste do painel driblava a
        trava que vale em produção."""
        bico, motivo = banco.bico_verificar_ativo(posto["bico_id"])
        assert motivo is None and bico is not None

        with banco.cursor() as c:
            c.execute("UPDATE automacoes SET ativo=0 WHERE id=?", (posto["automacao_id"],))
        _bico, motivo = banco.bico_verificar_ativo(posto["bico_id"])
        assert motivo == "automacao_inativa"


class TestIsolamentoEntrePostos:
    def test_bico_nao_aponta_para_camera_de_outro_posto(self, admin, posto):
        """Num servidor central isso entregaria a imagem do pátio de um cliente para
        o roteador de outro."""
        outra_ent = admin.post("/api/entidades", json={"nome": "Rede B"}).json()["id"]
        outra_emp = admin.post("/api/empresas", json={
            "entidade_id": outra_ent, "nome": "Posto B", "cnpj": "45723174000110"}).json()["id"]
        outra_auto = admin.post("/api/automacoes", json={
            "empresa_id": outra_emp, "codigo": "1"}).json()["id"]

        r = admin.post("/api/bicos", json={
            "automacao_id": outra_auto, "codigo": "9", "camera_id": posto["camera_id"]})
        assert r.status_code == 400
        assert "mesmo posto" in r.json()["detail"]

    def test_codigo_de_bico_repetido_na_mesma_automacao_da_conflito(self, admin, posto):
        r = admin.post("/api/bicos", json={
            "automacao_id": posto["automacao_id"], "codigo": "3",
            "camera_id": posto["camera_id"]})
        assert r.status_code == 409


class TestExclusaoEmCascata:
    def test_remover_entidade_leva_junto_posto_camera_e_bico(self, admin, posto):
        assert admin.delete(f"/api/entidades/{posto['entidade_id']}").status_code == 200
        assert banco.empresas_obter(posto["empresa_id"]) is None
        assert banco.bicos_obter(posto["bico_id"]) is None
        assert banco.cameras_obter(posto["camera_id"]) is None

    def test_camera_em_uso_nao_pode_ser_removida_avulsa(self, admin, posto):
        r = admin.delete(f"/api/cameras/{posto['camera_id']}")
        assert r.status_code == 409
        assert banco.cameras_obter(posto["camera_id"]) is not None
