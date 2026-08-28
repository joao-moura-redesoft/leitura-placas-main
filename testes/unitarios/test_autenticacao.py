"""Login, sessão e freio de força bruta.

Vários destes cobrem bugs que existiram de verdade: conta desativada que continuava
logando, sessão que sobrevivia à exclusão do usuário, e sessão que morria a cada
restart do servidor.
"""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.core import banco
from app.seguranca import sessao as auth_mod
from app.seguranca import tentativas


def _login(cli: TestClient, email: str, senha: str):
    return cli.post("/login", data={"email": email, "senha": senha}, follow_redirects=False)


class TestLogin:
    def test_credencial_correta_entra_e_recebe_cookie(self, admin, ambiente):
        from app.servidor import app
        cli = TestClient(app)
        r = _login(cli, "admin@teste.com", "senha-admin-1")
        assert r.status_code == 303
        assert r.headers["location"] == "/postos"
        assert r.cookies.get("sessao")

    def test_senha_errada_nao_entra(self, admin, ambiente):
        from app.servidor import app
        r = _login(TestClient(app), "admin@teste.com", "senha-errada")
        assert r.status_code == 200          # volta pro formulário
        assert not r.cookies.get("sessao")

    def test_conta_desativada_nao_entra(self, admin, cliente_logado, posto, ambiente):
        """Regressão: `ativo` era gravado mas nunca consultado no login — desativar
        um usuário no painel não impedia o login dele."""
        uid = admin.get("/api/usuarios").json()[-1]["id"]
        admin.put(f"/api/usuarios/{uid}", json={
            "nome": "Cliente", "email": "cliente@teste.com", "papel": "cliente",
            "empresa_id": posto["empresa_id"], "ativo": False,
        })
        from app.servidor import app
        r = _login(TestClient(app), "cliente@teste.com", "senha-cliente-1")
        assert not r.cookies.get("sessao")
        assert "incorretos" in r.text


class TestSessao:
    def test_sessao_sobrevive_a_reinicio_do_servidor(self, admin, ambiente):
        """Regressão: as sessões viviam num dict em memória, então todo restart
        deslogava todo mundo mesmo com o cookie ainda válido no navegador."""
        token = admin.cookies["sessao"]
        assert admin.get("/api/stats").status_code == 200

        from app.servidor import app
        novo = TestClient(app)               # processo "novo", mesmo banco
        novo.cookies.set("sessao", token)
        assert novo.get("/api/stats").status_code == 200

    def test_logout_invalida_o_token(self, admin):
        token = admin.cookies["sessao"]
        admin.get("/logout", follow_redirects=False)
        admin.cookies.set("sessao", token)
        assert admin.get("/api/stats").status_code == 401

    def test_desativar_usuario_derruba_a_sessao_aberta(self, admin, cliente_logado, posto):
        """Regressão: a sessão não era confrontada com o estado da conta, então
        desativar alguém não tirava quem já estava dentro."""
        assert cliente_logado.get("/api/postos").status_code == 200
        uid = admin.get("/api/usuarios").json()[-1]["id"]
        admin.put(f"/api/usuarios/{uid}", json={
            "nome": "Cliente", "email": "cliente@teste.com", "papel": "cliente",
            "empresa_id": posto["empresa_id"], "ativo": False,
        })
        assert cliente_logado.get("/api/postos").status_code == 401

    def test_rebaixar_papel_derruba_a_sessao(self, admin, posto, ambiente):
        """Sem isto o navegador já aberto continuaria operando como admin depois do
        rebaixamento — `ativo` continua 1, então nada mais o barraria."""
        r = admin.post("/api/usuarios", json={
            "nome": "Dois", "email": "d@teste.com", "senha": "senha-dois-12", "papel": "admin"})
        uid = r.json()["id"]
        from app.servidor import app
        cli = TestClient(app)
        cli.cookies.set("sessao", _login(cli, "d@teste.com", "senha-dois-12").cookies["sessao"])
        assert cli.get("/api/usuarios").status_code == 200

        admin.put(f"/api/usuarios/{uid}", json={
            "nome": "Dois", "email": "d@teste.com", "papel": "cliente",
            "empresa_id": posto["empresa_id"], "ativo": True})
        assert cli.get("/api/stats").status_code == 401

    def test_trocar_a_propria_senha_nao_expulsa_quem_trocou(self, admin):
        eu = admin.get("/api/usuarios/eu").json()
        r = admin.put(f"/api/usuarios/{eu['id']}", json={
            "nome": eu["nome"], "email": eu["email"], "papel": "admin",
            "ativo": True, "senha": "senha-nova-123",
        })
        assert r.status_code == 200
        assert admin.get("/api/stats").status_code == 200

    def test_token_desconhecido_e_recusado(self, admin, ambiente):
        from app.servidor import app
        cli = TestClient(app)
        cli.cookies.set("sessao", "token-inventado")
        assert cli.get("/api/stats").status_code == 401

    def test_sessao_expirada_e_recusada_e_apagada(self, admin, ambiente):
        token = admin.cookies["sessao"]
        banco.sessao_renovar(token, time.time() - 1)
        assert admin.get("/api/stats").status_code == 401
        assert banco.sessao_resolver(token) is None


class TestFreioDeForcaBruta:
    def test_libera_enquanto_esta_abaixo_do_limite(self, admin, ambiente):
        from app.servidor import app
        cli = TestClient(app)
        for _ in range(4):
            _login(cli, "admin@teste.com", "errada")
        # ainda consegue entrar com a senha certa
        assert _login(cli, "admin@teste.com", "senha-admin-1").cookies.get("sessao")

    def test_bloqueia_depois_de_seguidas_falhas(self, admin, ambiente):
        from app.servidor import app
        cli = TestClient(app)
        for _ in range(6):
            _login(cli, "admin@teste.com", "errada")
        r = _login(cli, "admin@teste.com", "senha-admin-1")
        assert not r.cookies.get("sessao"), "senha correta deveria estar bloqueada"
        assert "Muitas tentativas" in r.text

    def test_sucesso_zera_o_contador(self):
        tentativas._resetar_para_teste()
        for _ in range(3):
            tentativas.registrar_falha("a@t.com", "1.2.3.4")
        tentativas.registrar_sucesso("a@t.com", "1.2.3.4")
        for _ in range(3):
            tentativas.registrar_falha("a@t.com", "1.2.3.4")
        assert tentativas.segundos_de_bloqueio("a@t.com", "1.2.3.4") == 0

    def test_bloqueia_por_ip_mesmo_variando_o_email(self):
        """Só contar por e-mail deixaria varrer muitos e-mails a partir de um IP."""
        tentativas._resetar_para_teste()
        for i in range(6):
            tentativas.registrar_falha(f"vitima{i}@t.com", "9.9.9.9")
        assert tentativas.segundos_de_bloqueio("outro@t.com", "9.9.9.9") > 0

    def test_bloqueia_por_email_mesmo_variando_o_ip(self):
        """Só contar por IP deixaria uma botnet atacar a mesma conta de vários lugares."""
        tentativas._resetar_para_teste()
        for i in range(6):
            tentativas.registrar_falha("alvo@t.com", f"10.0.0.{i}")
        assert tentativas.segundos_de_bloqueio("alvo@t.com", "10.0.0.99") > 0

    def test_espera_cresce_a_cada_nova_falha(self):
        tentativas._resetar_para_teste()
        for _ in range(6):
            tentativas.registrar_falha("a@t.com", "1.1.1.1")
        primeira = tentativas.segundos_de_bloqueio("a@t.com", "1.1.1.1")
        tentativas.registrar_falha("a@t.com", "1.1.1.1")
        assert tentativas.segundos_de_bloqueio("a@t.com", "1.1.1.1") > primeira


class TestBootstrapDoPrimeiroAdmin:
    def test_sem_usuarios_qualquer_pagina_leva_para_criar_admin(self, cliente):
        r = cliente.get("/postos", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/criar-admin"

    def test_primeiro_usuario_nasce_admin(self, admin):
        assert admin.get("/api/usuarios/eu").json()["papel"] == "admin"

    def test_criar_admin_fecha_depois_do_primeiro(self, admin):
        r = admin.get("/criar-admin", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login"

    def test_senha_curta_e_recusada(self, cliente):
        r = cliente.post("/criar-admin", follow_redirects=False, data={
            "nome": "A", "email": "a@t.com", "senha": "1234", "confirmar": "1234"})
        assert r.status_code == 303
        assert r.headers["location"] == "/criar-admin"
        assert banco.contar_usuarios() == 0

    def test_senhas_diferentes_sao_recusadas(self, cliente):
        cliente.post("/criar-admin", follow_redirects=False, data={
            "nome": "A", "email": "a@t.com", "senha": "senha-boa-123", "confirmar": "outra-coisa-1"})
        assert banco.contar_usuarios() == 0


class TestTrocaDeSenhaSelfService:
    """Qualquer usuário logado (admin, operador ou cliente) troca a PRÓPRIA senha sem
    depender de admin — ver app/web/usuarios.py:trocar_a_propria_senha."""

    def test_qualquer_usuario_logado_troca_a_propria_senha(self, admin):
        r = admin.post("/api/usuarios/eu/senha", json={
            "senha_atual": "senha-admin-1", "senha_nova": "senha-nova-do-admin-1"})
        assert r.status_code == 200
        assert admin.get("/api/stats").status_code == 200  # sessão atual continua valendo

    def test_cliente_tambem_troca_a_propria_senha(self, cliente_logado):
        r = cliente_logado.post("/api/usuarios/eu/senha", json={
            "senha_atual": "senha-cliente-1", "senha_nova": "senha-nova-do-cliente-1"})
        assert r.status_code == 200
        assert cliente_logado.get("/api/postos").status_code == 200

    def test_senha_atual_errada_e_recusada(self, admin):
        r = admin.post("/api/usuarios/eu/senha", json={
            "senha_atual": "senha-errada", "senha_nova": "senha-nova-123"})
        assert r.status_code == 400

    def test_nova_senha_curta_e_recusada(self, admin):
        r = admin.post("/api/usuarios/eu/senha", json={
            "senha_atual": "senha-admin-1", "senha_nova": "123"})
        assert r.status_code == 400

    def test_deixa_de_entrar_com_a_senha_antiga(self, admin, ambiente):
        admin.post("/api/usuarios/eu/senha", json={
            "senha_atual": "senha-admin-1", "senha_nova": "senha-nova-do-admin-1"})
        from app.servidor import app
        r = _login(TestClient(app), "admin@teste.com", "senha-admin-1")
        assert not r.cookies.get("sessao")

    def test_derruba_as_outras_sessoes_mas_preserva_a_atual(self, admin):
        from app.servidor import app
        outra = TestClient(app)
        r = _login(outra, "admin@teste.com", "senha-admin-1")
        outra.cookies.set("sessao", r.cookies["sessao"])
        assert outra.get("/api/stats").status_code == 200

        admin.post("/api/usuarios/eu/senha", json={
            "senha_atual": "senha-admin-1", "senha_nova": "senha-nova-do-admin-1"})

        assert admin.get("/api/stats").status_code == 200   # sessão que trocou continua
        assert outra.get("/api/stats").status_code == 401   # a outra sessão morreu


def test_hash_de_senha_nao_guarda_texto_puro():
    h = auth_mod.hash_senha("minha-senha-secreta")
    assert h != "minha-senha-secreta"
    assert auth_mod.verificar_senha("minha-senha-secreta", h)
    assert not auth_mod.verificar_senha("outra-senha", h)
