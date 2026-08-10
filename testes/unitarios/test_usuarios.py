"""CRUD de usuários e as travas que impedem alguém de se trancar para fora.

Adaptado do design original (papel genérico 'usuario', sem escopo por posto, com
DELETE de usuário) para o que ganhou o merge: só 'admin' e 'cliente' (este último
sempre preso a um posto via `empresa_id`), sem exclusão definitiva — só desativação
(`ativo=False`, reversível, preserva o registro pro histórico/auditoria).
"""
from __future__ import annotations

from app.core import banco


def _ids_por_email(admin) -> dict[str, int]:
    return {u["email"]: u["id"] for u in admin.get("/api/usuarios").json()}


class TestListagem:
    def test_nunca_devolve_o_hash_da_senha(self, admin, cliente_logado):
        for u in admin.get("/api/usuarios").json():
            assert "senha" not in u
        assert "senha" not in admin.get("/api/usuarios/eu").json()

    def test_eu_identifica_quem_esta_logado(self, admin):
        assert admin.get("/api/usuarios/eu").json()["email"] == "admin@teste.com"


class TestCriacao:
    def test_cria_usuario_cliente_preso_a_um_posto(self, admin, posto):
        r = admin.post("/api/usuarios", json={
            "nome": "Novo", "email": "novo@teste.com", "senha": "senha-boa-123",
            "papel": "cliente", "empresa_id": posto["empresa_id"]})
        assert r.status_code == 200
        criado = banco.buscar_usuario_id(r.json()["id"])
        assert criado["papel"] == "cliente"
        assert criado["empresa_id"] == posto["empresa_id"]

    def test_cliente_sem_empresa_id_e_recusado(self, admin):
        r = admin.post("/api/usuarios", json={
            "nome": "Novo", "email": "semposto@teste.com", "senha": "senha-boa-123",
            "papel": "cliente"})
        assert r.status_code == 400

    def test_email_duplicado_da_conflito(self, admin):
        admin.post("/api/usuarios", json={
            "nome": "A", "email": "dup@teste.com", "senha": "senha-boa-123", "papel": "admin"})
        r = admin.post("/api/usuarios", json={
            "nome": "B", "email": "dup@teste.com", "senha": "senha-boa-123", "papel": "admin"})
        assert r.status_code == 409

    def test_email_e_normalizado_para_minusculas(self, admin):
        admin.post("/api/usuarios", json={
            "nome": "C", "email": "  MAIUSCULA@Teste.COM  ", "senha": "senha-boa-123", "papel": "admin"})
        assert banco.buscar_usuario_email("maiuscula@teste.com") is not None

    def test_senha_curta_e_recusada(self, admin):
        r = admin.post("/api/usuarios", json={
            "nome": "D", "email": "d@teste.com", "senha": "1234", "papel": "admin"})
        assert r.status_code == 400

    def test_papel_invalido_e_recusado(self, admin):
        r = admin.post("/api/usuarios", json={
            "nome": "E", "email": "e@teste.com", "senha": "senha-boa-123", "papel": "superusuario"})
        assert r.status_code == 400

    def test_nome_e_email_sao_obrigatorios(self, admin):
        assert admin.post("/api/usuarios", json={
            "nome": "", "email": "f@teste.com", "senha": "senha-boa-123", "papel": "admin"}).status_code == 400
        assert admin.post("/api/usuarios", json={
            "nome": "F", "email": "  ", "senha": "senha-boa-123", "papel": "admin"}).status_code == 400

    def test_senha_e_guardada_com_hash(self, admin):
        admin.post("/api/usuarios", json={
            "nome": "G", "email": "g@teste.com", "senha": "senha-boa-123", "papel": "admin"})
        assert banco.buscar_usuario_email("g@teste.com")["senha"] != "senha-boa-123"


class TestEdicao:
    def test_altera_nome_e_papel(self, admin, cliente_logado):
        uid = _ids_por_email(admin)["cliente@teste.com"]
        r = admin.put(f"/api/usuarios/{uid}", json={
            "nome": "Renomeado", "email": "cliente@teste.com", "papel": "admin", "ativo": True})
        assert r.status_code == 200
        renomeado = banco.buscar_usuario_id(uid)
        assert renomeado["nome"] == "Renomeado"
        assert renomeado["papel"] == "admin"

    def test_senha_vazia_mantem_a_senha_atual(self, admin, cliente_logado, posto):
        uid = _ids_por_email(admin)["cliente@teste.com"]
        antes = banco.buscar_usuario_id(uid)["senha"]
        admin.put(f"/api/usuarios/{uid}", json={
            "nome": "Cliente", "email": "cliente@teste.com", "papel": "cliente",
            "empresa_id": posto["empresa_id"], "ativo": True})
        assert banco.buscar_usuario_id(uid)["senha"] == antes

    def test_senha_preenchida_troca_a_senha(self, admin, cliente_logado, posto):
        uid = _ids_por_email(admin)["cliente@teste.com"]
        antes = banco.buscar_usuario_id(uid)["senha"]
        admin.put(f"/api/usuarios/{uid}", json={
            "nome": "Cliente", "email": "cliente@teste.com", "papel": "cliente",
            "empresa_id": posto["empresa_id"], "ativo": True, "senha": "senha-trocada-1"})
        assert banco.buscar_usuario_id(uid)["senha"] != antes

    def test_email_duplicado_na_edicao_da_conflito(self, admin, cliente_logado, posto):
        uid = _ids_por_email(admin)["cliente@teste.com"]
        r = admin.put(f"/api/usuarios/{uid}", json={
            "nome": "Cliente", "email": "admin@teste.com", "papel": "cliente",
            "empresa_id": posto["empresa_id"], "ativo": True})
        assert r.status_code == 409

    def test_usuario_inexistente_da_404(self, admin):
        r = admin.put("/api/usuarios/99999", json={
            "nome": "X", "email": "x@teste.com", "papel": "admin", "ativo": True})
        assert r.status_code == 404


class TestTravasDeAdministrador:
    """Sem estas travas o sistema fica sem nenhum administrador — e ninguém consegue
    mais entrar no painel para consertar."""

    def test_nao_rebaixa_o_proprio_acesso(self, admin, posto):
        eu = admin.get("/api/usuarios/eu").json()
        r = admin.put(f"/api/usuarios/{eu['id']}", json={
            "nome": eu["nome"], "email": eu["email"], "papel": "cliente",
            "empresa_id": posto["empresa_id"], "ativo": True})
        assert r.status_code == 400
        assert banco.buscar_usuario_id(eu["id"])["papel"] == "admin"

    def test_nao_desativa_o_proprio_usuario(self, admin):
        eu = admin.get("/api/usuarios/eu").json()
        r = admin.put(f"/api/usuarios/{eu['id']}", json={
            "nome": eu["nome"], "email": eu["email"], "papel": "admin", "ativo": False})
        assert r.status_code == 400
        assert banco.buscar_usuario_id(eu["id"])["ativo"] == 1

    def test_nao_desativa_o_ultimo_admin_via_api_key(self, admin):
        """A trava de "não fica sem nenhum admin" existe ALÉM da autoproteção acima
        (que só cobre auto-edição via sessão) — cobre também quem usa a api_key global
        do servidor (sem usuário/sessão associada, ver deps.py:usuario_atual devolve
        None) tentando desativar o único admin restante."""
        from app.core import config
        config.salvar({**config.carregar(), "api_key": "chave-de-teste-123"})
        eu = admin.get("/api/usuarios/eu").json()

        from app.servidor import app
        from fastapi.testclient import TestClient
        anon = TestClient(app)
        r = anon.put(f"/api/usuarios/{eu['id']}", json={
            "nome": eu["nome"], "email": eu["email"], "papel": "admin", "ativo": False},
            headers={"X-API-Key": "chave-de-teste-123"})
        assert r.status_code == 400
        assert banco.usuarios_contar_admins_ativos() == 1

    def test_admin_pode_rebaixar_outro_admin_se_sobrar_um(self, admin, posto):
        outro = admin.post("/api/usuarios", json={
            "nome": "Outro", "email": "outro@teste.com",
            "senha": "senha-outro-1", "papel": "admin"}).json()["id"]
        r = admin.put(f"/api/usuarios/{outro}", json={
            "nome": "Outro", "email": "outro@teste.com", "papel": "cliente",
            "empresa_id": posto["empresa_id"], "ativo": True})
        assert r.status_code == 200


class TestContagemDeAdmins:
    def test_conta_apenas_admins_ativos(self, ambiente):
        banco.criar_usuario("A", "a@t.com", "h", papel="admin", ativo=1)
        banco.criar_usuario("B", "b@t.com", "h", papel="admin", ativo=0)
        banco.criar_usuario("C", "c@t.com", "h", papel="usuario", ativo=1)
        assert banco.usuarios_contar_admins_ativos() == 1

    def test_exclui_o_id_informado(self, ambiente):
        uid = banco.criar_usuario("A", "a@t.com", "h", papel="admin", ativo=1)
        assert banco.usuarios_contar_admins_ativos(excluir_id=uid) == 0

    def test_email_duplicado_devolve_none(self, ambiente):
        banco.criar_usuario("A", "a@t.com", "h")
        assert banco.criar_usuario("B", "a@t.com", "h") is None
