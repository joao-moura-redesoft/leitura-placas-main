"""Gate de papel: quem só opera não altera o sistema.

A regra é aplicada no middleware por MÉTODO (qualquer POST/PUT/DELETE exige admin,
salvo uma lista curta de escritas operacionais), então o mais importante aqui é provar
que ela pega rotas que ninguém anotou uma a uma — inclusive as que vierem depois.
"""
from __future__ import annotations

import pytest

from app.core import config


class TestOperadorNaoAltera:
    @pytest.mark.parametrize("metodo, rota, corpo", [
        ("post",   "/api/config",     {"log_level": "debug"}),
        ("post",   "/api/entidades",  {"nome": "Nova"}),
        ("post",   "/api/empresas",   {"nome": "X", "cnpj": "11222333000181", "entidade_id": 1}),
        ("post",   "/api/cameras",    {"nome": "Cam", "empresa_id": 1}),
        ("post",   "/api/listas",     {"placa": "ABC1D23", "tipo": "negra"}),
        ("put",    "/api/entidades/1", {"nome": "Outra"}),
        ("delete", "/api/entidades/1", None),
        ("delete", "/api/cameras/1",   None),
        ("delete", "/api/deteccoes/1", None),
        ("delete", "/api/logs",        None),
        ("post",   "/api/usuarios",   {"nome": "N", "email": "n@t.com", "senha": "12345678"}),
        ("delete", "/api/usuarios/1", None),
    ])
    def test_escrita_e_recusada(self, operador, metodo, rota, corpo):
        r = getattr(operador, metodo)(rota, json=corpo) if corpo is not None \
            else getattr(operador, metodo)(rota)
        assert r.status_code == 403, f"{metodo.upper()} {rota} deveria exigir admin"

    def test_config_nao_muda_de_verdade(self, operador, admin):
        """Não basta responder 403 — o efeito colateral não pode ter acontecido."""
        antes = config.carregar().get("log_level")
        operador.post("/api/config", json={"log_level": "debug"})
        assert config.carregar().get("log_level") == antes

    @pytest.mark.parametrize("rota", [
        "/configuracao", "/usuarios", "/entidades", "/empresas",
        "/automacoes", "/bicos", "/cameras", "/testes",
    ])
    def test_pagina_de_administracao_redireciona(self, operador, rota):
        r = operador.get(rota, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/postos"

    def test_nav_esconde_o_que_ele_nao_pode_abrir(self, operador):
        html = operador.get("/postos").text
        assert "/configuracao" not in html
        assert "/usuarios" not in html
        assert "/documentacao" in html, "o que é permitido continua visível"

    def test_rota_de_escrita_nova_nasce_protegida(self, operador):
        """O gate é por método, não por lista de rotas: uma rota de escrita que
        ninguém lembrou de anotar já cai no 403 em vez de ficar aberta."""
        r = operador.post("/api/rota-que-nao-existe-ainda", json={})
        assert r.status_code == 403      # 403 antes de virar 404


class TestOperadorOpera:
    @pytest.mark.parametrize("rota", [
        "/api/stats", "/api/deteccoes", "/api/chamadas", "/api/status",
        "/api/listas", "/api/cameras", "/api/entidades", "/api/postos",
    ])
    def test_leitura_liberada(self, operador, rota):
        assert operador.get(rota).status_code == 200

    @pytest.mark.parametrize("rota", ["/postos", "/historico", "/listas", "/dashboard"])
    def test_paginas_de_operacao_abrem(self, operador, rota):
        assert operador.get(rota).status_code == 200

    def test_disparar_leitura_de_bico_e_operacao_nao_administracao(self, operador):
        """Ler placa é o trabalho do operador, então esta escrita passa pelo gate.

        Usa um bico inexistente de propósito: a rota responde 404 antes de tocar em
        câmera ou modelo, e o que este teste precisa provar é só que a resposta NÃO é 403.
        """
        r = operador.post("/api/bicos/999999/ler-placa-teste")
        assert r.status_code == 404


class TestAdminPodeTudo:
    def test_altera_configuracao(self, admin):
        assert admin.post("/api/config", json={"log_level": "info"}).status_code == 200

    def test_abre_as_paginas_de_administracao(self, admin):
        for rota in ("/configuracao", "/usuarios", "/entidades"):
            assert admin.get(rota).status_code == 200

    def test_nav_mostra_administracao(self, admin):
        html = admin.get("/postos").text
        assert "/usuarios" in html
        assert "/configuracao" in html


class TestApiKey:
    def test_leitura_reativa_exige_chave(self, admin, posto):
        """Regressão: `/api/leitura` era público — qualquer um disparava leitura para
        qualquer CNPJ e recebia a placa."""
        from app.servidor import app
        from fastapi.testclient import TestClient
        anon = TestClient(app)
        r = anon.get("/api/leitura", params={
            "entidade": "Rede Teste", "cnpj": posto["cnpj"], "automacao": "1", "bico": "3"})
        assert r.status_code == 401

    def test_chave_valida_passa_pela_autenticacao(self, admin, posto):
        from app.servidor import app
        from fastapi.testclient import TestClient
        cfg = config.carregar()
        config.salvar({**cfg, "api_key": "chave-de-teste-123"})

        anon = TestClient(app)
        r = anon.get("/api/leitura", params={
            "entidade": "Rede Teste", "cnpj": "00000000000000",
            "automacao": "1", "bico": "3", "api_key": "chave-de-teste-123"})
        # 404 = autenticou e chegou na resolução do cadastro (CNPJ inexistente de propósito,
        # para não disparar câmera/modelo de verdade dentro da suíte).
        assert r.status_code == 404

    def test_chave_errada_nao_passa(self, admin):
        from app.servidor import app
        from fastapi.testclient import TestClient
        config.salvar({**config.carregar(), "api_key": "chave-de-teste-123"})
        anon = TestClient(app)
        r = anon.get("/api/leitura", params={
            "entidade": "x", "cnpj": "1", "automacao": "1", "bico": "1", "api_key": "errada"})
        assert r.status_code == 401

    def test_healthz_continua_publico(self, admin):
        """O healthcheck do container não tem sessão nem chave — se ele exigir auth,
        o orquestrador marca o container como unhealthy para sempre."""
        from app.servidor import app
        from fastapi.testclient import TestClient
        assert TestClient(app).get("/api/healthz").status_code == 200
