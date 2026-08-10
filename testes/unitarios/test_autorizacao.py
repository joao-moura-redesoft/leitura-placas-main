"""Gate de admin e api_key por posto.

Histórico: esta suíte testava originalmente um design de RBAC por MIDDLEWARE (papel
'operador' genérico + api_key global obrigatória por padrão em `/api/leitura`) que foi
construído numa branch paralela e NÃO sobreviveu ao merge com o design que ganhou:
escopo por 'cliente' restrito a um posto (app/web/deps.py) reforçado rota a rota, e
api_key OPT-IN por posto (não global obrigatória — `/api/leitura` continua público por
padrão, decisão registrada no README e em docs/ARQUITETURA.md). Reescrita para testar
o que está de pé.
"""
from __future__ import annotations

from fastapi.testclient import TestClient


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


class TestClienteNaoAltera:
    """Usuário 'cliente' (restrito a um posto) não alcança cadastro estrutural nem
    configuração do sistema — ver app/web/deps.py:exigir_admin."""

    def test_nao_cria_entidade(self, cliente_logado):
        assert cliente_logado.post("/api/entidades", json={"nome": "X"}).status_code == 403

    def test_nao_altera_configuracao(self, cliente_logado):
        assert cliente_logado.post("/api/config", json={"log_level": "debug"}).status_code == 403

    def test_nao_ve_lista_de_usuarios(self, cliente_logado):
        assert cliente_logado.get("/api/usuarios").status_code == 403

    def test_paginas_de_administracao_mandam_de_volta_pro_postos(self, cliente_logado):
        r = cliente_logado.get("/configuracao", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/postos"

    def test_nav_esconde_o_que_ele_nao_pode_abrir(self, cliente_logado):
        html = cliente_logado.get("/postos").text
        assert "/configuracao" not in html
        assert "/usuarios" not in html

    def test_ve_so_o_proprio_posto(self, cliente_logado, admin, posto):
        # Um segundo posto de outro cliente
        ent2 = admin.post("/api/entidades", json={"nome": "Rede Y"}).json()["id"]
        admin.post("/api/empresas", json={
            "entidade_id": ent2, "nome": "Posto 2", "cnpj": "11444777000161"})

        nomes = [p["nome"] for p in cliente_logado.get("/api/postos").json()]
        assert nomes == ["Posto 1"]


class TestApiKeyPorPosto:
    """`/api/leitura` é público por padrão; api_key só passa a ser exigida quando o
    POSTO TEM uma chave própria configurada (opt-in) — não existe api_key global
    obrigatória por padrão. Câmera do posto de teste é falsa de propósito
    (`rtsp://x/1` — ver fixture `posto`), então uma chamada que passa da autenticação
    falha na conexão (503), nunca chega a carregar detector/OCR de verdade."""

    def _get(self, params: dict):
        from app.servidor import app
        return TestClient(app).get("/api/leitura", params=params)

    def test_publico_por_padrao_sem_chave_no_posto(self, admin, posto):
        r = self._get({"entidade": "Rede Teste", "cnpj": posto["cnpj"],
                       "automacao": "1", "bico": "3"})
        assert r.status_code == 503  # passou da autenticação, falhou na câmera falsa

    def test_exige_chave_quando_o_posto_tem_uma(self, admin, posto):
        admin.post(f"/api/empresas/{posto['empresa_id']}/api-key")
        r = self._get({"entidade": "Rede Teste", "cnpj": posto["cnpj"],
                       "automacao": "1", "bico": "3"})
        # 404, não 401: não confirma que o cadastro existe pra quem não tem a chave
        # (mesmo padrão de app/core/banco.py:resolver_bico para CNPJ desconhecido).
        assert r.status_code == 404

    def test_chave_errada_e_recusada(self, admin, posto):
        admin.post(f"/api/empresas/{posto['empresa_id']}/api-key")
        r = self._get({"entidade": "Rede Teste", "cnpj": posto["cnpj"],
                       "automacao": "1", "bico": "3", "api_key": "chave-errada"})
        assert r.status_code == 404

    def test_chave_correta_passa_da_autenticacao(self, admin, posto):
        chave = admin.post(f"/api/empresas/{posto['empresa_id']}/api-key").json()["api_key"]
        r = self._get({"entidade": "Rede Teste", "cnpj": posto["cnpj"],
                       "automacao": "1", "bico": "3", "api_key": chave})
        assert r.status_code == 503  # mesma câmera falsa de sempre — não é mais 404

    def test_revogar_a_chave_volta_ao_publico(self, admin, posto):
        """Checa o efeito no banco, não via HTTP: uma chamada real de `/api/leitura`
        sem chave (posto público) tenta conectar na câmera falsa e só responde depois
        de ~30s de timeout de rede — as duas outras já provam esse caminho."""
        from app.core import banco
        admin.post(f"/api/empresas/{posto['empresa_id']}/api-key")
        admin.delete(f"/api/empresas/{posto['empresa_id']}/api-key")
        assert banco.empresas_obter(posto["empresa_id"])["api_key"] == ""

    def test_healthz_continua_publico(self):
        from app.servidor import app
        assert TestClient(app).get("/api/healthz").status_code == 200
