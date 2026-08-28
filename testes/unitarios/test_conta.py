"""Política de senha, esqueci-minha-senha, convite por e-mail, sessões ativas e
auditoria — tudo adicionado numa sessão só, cobrindo o caminho feliz e as travas de
cada recurso."""
from __future__ import annotations

import app.seguranca.email as email_real
from fastapi.testclient import TestClient

from app.core import banco, config
from app.seguranca import sessao as auth_mod


def _login(cli, email, senha):
    return cli.post("/login", data={"email": email, "senha": senha}, follow_redirects=False)


class TestPoliticaDeSenha:
    def test_curta_e_recusada(self):
        assert auth_mod.senha_fraca("Ab1") is not None

    def test_uma_classe_so_e_recusada(self):
        assert auth_mod.senha_fraca("abcdefgh") is not None       # só letra
        assert auth_mod.senha_fraca("12345678") is not None       # só dígito

    def test_duas_classes_passa(self):
        assert auth_mod.senha_fraca("abcdefg1") is None
        assert auth_mod.senha_fraca("senha-boa-123") is None


class TestCookieSecure:
    def test_desligado_por_padrao(self, admin, ambiente):
        from app.servidor import app
        cli = TestClient(app)
        r = _login(cli, "admin@teste.com", "senha-admin-1")
        assert "Secure" not in r.headers.get("set-cookie", "")

    def test_ligado_via_config(self, admin, ambiente):
        config.salvar({**config.carregar(), "cookie_secure": "sim"})
        from app.servidor import app
        cli = TestClient(app)
        r = _login(cli, "admin@teste.com", "senha-admin-1")
        assert "Secure" in r.headers.get("set-cookie", "")


class TestEsqueciSenha:
    def test_sem_smtp_mostra_aviso_em_vez_de_enviar(self, cliente):
        r = cliente.post("/esqueci-senha", data={"email": "quemquer@teste.com"})
        assert r.status_code == 200
        assert "e-mail" in r.text.lower()

    def test_com_smtp_envia_e_o_link_funciona(self, admin, monkeypatch, ambiente):
        config.salvar({**config.carregar(), "smtp_host": "smtp.teste.local"})
        enviados = []
        monkeypatch.setattr(email_real, "enviar",
                            lambda dest, assunto, corpo, cfg=None: enviados.append((dest, corpo)) or True)

        from app.servidor import app
        anon = TestClient(app)
        r = anon.post("/esqueci-senha", data={"email": "admin@teste.com"})
        assert r.status_code == 200
        assert len(enviados) == 1
        dest, corpo = enviados[0]
        assert dest == "admin@teste.com"

        # extrai o token da linha do link no corpo do e-mail
        linha_link = next(l for l in corpo.splitlines() if "/redefinir-senha/" in l)
        token = linha_link.rsplit("/", 1)[-1]
        assert banco.reset_token_resolver(token) is not None

        r = anon.post(f"/redefinir-senha/{token}",
                      data={"senha": "senha-recuperada-1", "confirmar": "senha-recuperada-1"},
                      follow_redirects=False)
        assert r.status_code == 303
        assert r.cookies.get("sessao")  # loga automaticamente

        # token de uso único: não serve mais
        r2 = anon.get(f"/redefinir-senha/{token}")
        assert "inválido" in r2.text.lower() or "expirou" in r2.text.lower()

        # a senha antiga não funciona mais
        outro = TestClient(app)
        r3 = _login(outro, "admin@teste.com", "senha-admin-1")
        assert not r3.cookies.get("sessao")

    def test_nao_confirma_email_inexistente(self, admin, monkeypatch, ambiente):
        """Mesma resposta ("enviado") exista ou não a conta — não dá pra distinguir
        de fora."""
        config.salvar({**config.carregar(), "smtp_host": "smtp.teste.local"})
        enviados = []
        monkeypatch.setattr(email_real, "enviar",
                            lambda *a, **kw: enviados.append(1) or True)
        from app.servidor import app
        anon = TestClient(app)
        r1 = anon.post("/esqueci-senha", data={"email": "admin@teste.com"})
        r2 = anon.post("/esqueci-senha", data={"email": "nao-existe@teste.com"})
        assert r1.status_code == r2.status_code == 200
        assert len(enviados) == 1   # só o e-mail real disparou envio de verdade

    def test_token_invalido_mostra_mensagem_clara(self, cliente):
        r = cliente.get("/redefinir-senha/token-que-nao-existe")
        assert r.status_code == 200
        assert "inválido" in r.text.lower() or "expirou" in r.text.lower()


class TestConvitePorEmail:
    def test_sem_smtp_e_recusado(self, admin):
        r = admin.post("/api/usuarios", json={
            "nome": "Convidado", "email": "convidado@teste.com", "papel": "admin", "convidar": True})
        assert r.status_code == 400

    def test_com_smtp_cria_e_envia_convite(self, admin, monkeypatch):
        config.salvar({**config.carregar(), "smtp_host": "smtp.teste.local"})
        enviados = []
        monkeypatch.setattr(email_real, "enviar",
                            lambda dest, assunto, corpo, cfg=None: enviados.append((dest, corpo)) or True)

        r = admin.post("/api/usuarios", json={
            "nome": "Convidado", "email": "convidado@teste.com", "papel": "admin", "convidar": True})
        assert r.status_code == 200, r.text
        assert len(enviados) == 1
        dest, corpo = enviados[0]
        assert dest == "convidado@teste.com"

        linha_link = next(l for l in corpo.splitlines() if "/redefinir-senha/" in l)
        token = linha_link.rsplit("/", 1)[-1]
        from app.servidor import app
        anon = TestClient(app)
        r2 = anon.post(f"/redefinir-senha/{token}",
                       data={"senha": "senha-do-convidado-1", "confirmar": "senha-do-convidado-1"},
                       follow_redirects=False)
        assert r2.status_code == 303
        assert r2.cookies.get("sessao")


class TestSessoesAtivas:
    def test_lista_a_propria_sessao_como_atual(self, admin):
        r = admin.get("/api/usuarios/eu/sessoes")
        assert r.status_code == 200
        sessoes = r.json()
        assert len(sessoes) == 1
        assert sessoes[0]["atual"] is True

    def test_segunda_sessao_aparece_e_pode_ser_revogada(self, admin, ambiente):
        from app.servidor import app
        outra = TestClient(app)
        r = _login(outra, "admin@teste.com", "senha-admin-1")
        outra.cookies.set("sessao", r.cookies["sessao"])

        sessoes = admin.get("/api/usuarios/eu/sessoes").json()
        assert len(sessoes) == 2
        nao_atual = next(s for s in sessoes if not s["atual"])

        r = admin.delete(f"/api/usuarios/eu/sessoes/{nao_atual['token']}")
        assert r.status_code == 200
        assert outra.get("/api/stats").status_code == 401
        assert admin.get("/api/stats").status_code == 200  # a própria continua

    def test_nao_revoga_sessao_de_outro_usuario(self, admin, cliente_logado):
        token_do_cliente = cliente_logado.cookies["sessao"]
        r = admin.delete(f"/api/usuarios/eu/sessoes/{token_do_cliente}")
        assert r.status_code == 404
        assert cliente_logado.get("/api/postos").status_code == 200  # continua válida


class TestAuditoria:
    def test_bootstrap_do_primeiro_admin_gera_evento(self, admin):
        eventos = admin.get("/api/auditoria?acao=bootstrap_admin").json()
        assert any(e["acao"] == "bootstrap_admin" for e in eventos)

    def test_login_gera_evento(self, admin, ambiente):
        from app.servidor import app
        cli = TestClient(app)
        _login(cli, "admin@teste.com", "senha-admin-1")
        eventos = admin.get("/api/auditoria?acao=login").json()
        assert any(e["acao"] == "login" for e in eventos)

    def test_criar_usuario_gera_evento_com_alvo(self, admin, posto):
        r = admin.post("/api/usuarios", json={
            "nome": "X", "email": "x@teste.com", "senha": "senha-boa-123",
            "papel": "cliente", "empresa_id": posto["empresa_id"]})
        uid = r.json()["id"]
        eventos = admin.get(f"/api/auditoria?acao=usuario_criado&usuario_id={admin.get('/api/usuarios/eu').json()['id']}").json()
        assert any(e["alvo_id"] == str(uid) for e in eventos)

    def test_cliente_nao_acessa(self, cliente_logado):
        assert cliente_logado.get("/api/auditoria").status_code == 403

    def test_pagina_admin_abre(self, admin):
        assert admin.get("/auditoria").status_code == 200
