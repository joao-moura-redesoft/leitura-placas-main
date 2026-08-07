"""CRUD de usuários e as travas que impedem alguém de se trancar para fora."""
from __future__ import annotations

from app.core import banco


def _ids_por_email(admin) -> dict[str, int]:
    return {u["email"]: u["id"] for u in admin.get("/api/usuarios").json()}


class TestListagem:
    def test_nunca_devolve_o_hash_da_senha(self, admin, operador):
        for u in admin.get("/api/usuarios").json():
            assert "senha" not in u
        assert "senha" not in admin.get("/api/usuarios/eu").json()

    def test_eu_identifica_quem_esta_logado(self, admin):
        assert admin.get("/api/usuarios/eu").json()["email"] == "admin@teste.com"


class TestCriacao:
    def test_cria_usuario_comum(self, admin):
        r = admin.post("/api/usuarios", json={
            "nome": "Novo", "email": "novo@teste.com", "senha": "senha-boa-123", "papel": "usuario"})
        assert r.status_code == 200
        assert banco.buscar_usuario_id(r.json()["id"])["papel"] == "usuario"

    def test_email_duplicado_da_conflito(self, admin):
        admin.post("/api/usuarios", json={
            "nome": "A", "email": "dup@teste.com", "senha": "senha-boa-123"})
        r = admin.post("/api/usuarios", json={
            "nome": "B", "email": "dup@teste.com", "senha": "senha-boa-123"})
        assert r.status_code == 409

    def test_email_e_normalizado_para_minusculas(self, admin):
        admin.post("/api/usuarios", json={
            "nome": "C", "email": "  MAIUSCULA@Teste.COM  ", "senha": "senha-boa-123"})
        assert banco.buscar_usuario_email("maiuscula@teste.com") is not None

    def test_senha_curta_e_recusada(self, admin):
        r = admin.post("/api/usuarios", json={
            "nome": "D", "email": "d@teste.com", "senha": "1234"})
        assert r.status_code == 400

    def test_papel_invalido_e_recusado(self, admin):
        r = admin.post("/api/usuarios", json={
            "nome": "E", "email": "e@teste.com", "senha": "senha-boa-123", "papel": "superusuario"})
        assert r.status_code == 400

    def test_nome_e_email_sao_obrigatorios(self, admin):
        assert admin.post("/api/usuarios", json={
            "nome": "", "email": "f@teste.com", "senha": "senha-boa-123"}).status_code == 400
        assert admin.post("/api/usuarios", json={
            "nome": "F", "email": "  ", "senha": "senha-boa-123"}).status_code == 400

    def test_senha_e_guardada_com_hash(self, admin):
        admin.post("/api/usuarios", json={
            "nome": "G", "email": "g@teste.com", "senha": "senha-boa-123"})
        assert banco.buscar_usuario_email("g@teste.com")["senha"] != "senha-boa-123"


class TestEdicao:
    def test_altera_nome_e_papel(self, admin, operador):
        uid = _ids_por_email(admin)["op@teste.com"]
        r = admin.put(f"/api/usuarios/{uid}", json={
            "nome": "Renomeado", "email": "op@teste.com", "papel": "admin", "ativo": True})
        assert r.status_code == 200
        assert banco.buscar_usuario_id(uid)["nome"] == "Renomeado"

    def test_senha_vazia_mantem_a_senha_atual(self, admin, operador):
        uid = _ids_por_email(admin)["op@teste.com"]
        antes = banco.buscar_usuario_id(uid)["senha"]
        admin.put(f"/api/usuarios/{uid}", json={
            "nome": "Op", "email": "op@teste.com", "papel": "usuario", "ativo": True})
        assert banco.buscar_usuario_id(uid)["senha"] == antes

    def test_senha_preenchida_troca_a_senha(self, admin, operador):
        uid = _ids_por_email(admin)["op@teste.com"]
        antes = banco.buscar_usuario_id(uid)["senha"]
        admin.put(f"/api/usuarios/{uid}", json={
            "nome": "Op", "email": "op@teste.com", "papel": "usuario",
            "ativo": True, "senha": "senha-trocada-1"})
        assert banco.buscar_usuario_id(uid)["senha"] != antes

    def test_email_duplicado_na_edicao_da_conflito(self, admin, operador):
        uid = _ids_por_email(admin)["op@teste.com"]
        r = admin.put(f"/api/usuarios/{uid}", json={
            "nome": "Op", "email": "admin@teste.com", "papel": "usuario", "ativo": True})
        assert r.status_code == 409

    def test_usuario_inexistente_da_404(self, admin):
        r = admin.put("/api/usuarios/99999", json={
            "nome": "X", "email": "x@teste.com", "papel": "usuario", "ativo": True})
        assert r.status_code == 404


class TestTravasDeAdministrador:
    """Sem estas travas o sistema fica sem nenhum administrador — e ninguém consegue
    mais entrar no painel para consertar."""

    def test_nao_remove_o_proprio_usuario(self, admin):
        eu = admin.get("/api/usuarios/eu").json()["id"]
        r = admin.delete(f"/api/usuarios/{eu}")
        assert r.status_code == 400
        assert banco.buscar_usuario_id(eu) is not None

    def test_nao_rebaixa_o_proprio_acesso(self, admin):
        eu = admin.get("/api/usuarios/eu").json()
        r = admin.put(f"/api/usuarios/{eu['id']}", json={
            "nome": eu["nome"], "email": eu["email"], "papel": "usuario", "ativo": True})
        assert r.status_code == 400
        assert banco.buscar_usuario_id(eu["id"])["papel"] == "admin"

    def test_nao_desativa_o_proprio_usuario(self, admin):
        eu = admin.get("/api/usuarios/eu").json()
        r = admin.put(f"/api/usuarios/{eu['id']}", json={
            "nome": eu["nome"], "email": eu["email"], "papel": "admin", "ativo": False})
        assert r.status_code == 400

    def test_nao_remove_o_ultimo_admin(self, admin):
        """Outro admin remove o primeiro: permitido enquanto sobrar um ativo, e
        recusado quando não sobra."""
        outro = admin.post("/api/usuarios", json={
            "nome": "Outro", "email": "outro@teste.com",
            "senha": "senha-outro-1", "papel": "admin"}).json()["id"]
        primeiro = admin.get("/api/usuarios/eu").json()["id"]

        assert admin.delete(f"/api/usuarios/{outro}").status_code == 200
        # agora só resta o primeiro, que é ele mesmo — barrado pelas duas regras
        assert admin.delete(f"/api/usuarios/{primeiro}").status_code == 400
        assert banco.usuarios_contar_admins_ativos() == 1

    def test_admin_pode_rebaixar_outro_admin_se_sobrar_um(self, admin):
        outro = admin.post("/api/usuarios", json={
            "nome": "Outro", "email": "outro@teste.com",
            "senha": "senha-outro-1", "papel": "admin"}).json()["id"]
        r = admin.put(f"/api/usuarios/{outro}", json={
            "nome": "Outro", "email": "outro@teste.com", "papel": "usuario", "ativo": True})
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
