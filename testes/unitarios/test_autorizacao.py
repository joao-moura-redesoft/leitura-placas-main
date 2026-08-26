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


class TestOperadorVeTudoMasNaoAdministra:
    """'operador' não é preso a posto nenhum (vê tudo, como admin) mas não passa no
    gate de admin (não administra, como cliente) — ver app/web/deps.py."""

    def test_ve_todos_os_postos_nao_so_o_seu(self, operador_logado, admin, posto):
        ent2 = admin.post("/api/entidades", json={"nome": "Rede Y"}).json()["id"]
        admin.post("/api/empresas", json={
            "entidade_id": ent2, "nome": "Posto 2", "cnpj": "11444777000161"})
        nomes = {p["nome"] for p in operador_logado.get("/api/postos").json()}
        assert nomes == {"Posto 1", "Posto 2"}

    def test_nao_administra(self, operador_logado):
        assert operador_logado.post("/api/entidades", json={"nome": "X"}).status_code == 403
        assert operador_logado.post("/api/config", json={"log_level": "debug"}).status_code == 403
        assert operador_logado.get("/api/usuarios").status_code == 403

    def test_pagina_de_administracao_manda_de_volta_pro_postos(self, operador_logado):
        r = operador_logado.get("/configuracao", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/postos"


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


class TestPreviewComChaveDoPosto:
    """`/api/leitura` é público e devolve `frame_url` apontando para o preview do bico.

    O arquivo é privado (ver app/visao/leitura.py:PREVIEW_DIR — vazamento de 13/08), então
    a URL exige credencial. Sem aceitar a chave PRÓPRIA do posto, o payload anunciava ao
    roteador uma imagem que ele não tinha como buscar, e o fluxo recomendado quando
    `confirmada` vem false (mostrar a foto ao atendente) ficava impossível de construir.

    401 = barrado no middleware. 404 = passou da autenticação e chegou na rota (o preview
    em si não existe, porque nenhuma leitura rodou nestes testes) — é essa a distinção
    que interessa aqui.
    """

    def _get(self, bico_id: int, **params):
        from app.servidor import app
        return TestClient(app).get(f"/api/bicos/{bico_id}/preview.jpg", params=params)

    def test_sem_credencial_e_barrado(self, admin, posto):
        assert self._get(posto["bico_id"]).status_code == 401

    def test_chave_do_posto_libera(self, admin, posto):
        chave = admin.post(f"/api/empresas/{posto['empresa_id']}/api-key").json()["api_key"]
        r = self._get(posto["bico_id"], api_key=chave)
        assert r.status_code == 404, "a chave do posto deveria passar do middleware"

    def test_chave_errada_continua_barrada(self, admin, posto):
        admin.post(f"/api/empresas/{posto['empresa_id']}/api-key")
        assert self._get(posto["bico_id"], api_key="chave-errada").status_code == 401

    def test_chave_de_outro_posto_nao_abre_este_bico(self, admin, posto):
        """Escopo estreito: a chave do posto A não pode abrir o preview do posto B."""
        ent_b = admin.post("/api/entidades", json={"nome": "Rede B"}).json()["id"]
        emp_b = admin.post("/api/empresas", json={
            "entidade_id": ent_b, "nome": "Posto B", "cnpj": "45723174000110"}).json()["id"]
        chave_b = admin.post(f"/api/empresas/{emp_b}/api-key").json()["api_key"]
        admin.post(f"/api/empresas/{posto['empresa_id']}/api-key")
        assert self._get(posto["bico_id"], api_key=chave_b).status_code == 401

    def test_posto_sem_chave_nao_fica_publico(self, admin, posto):
        """O preview NUNCA vira público: sem chave configurada, só sessão do painel abre.
        É o que impede voltar a dar para iterar bico_id sem autenticação nenhuma."""
        assert self._get(posto["bico_id"]).status_code == 401
        assert self._get(posto["bico_id"], api_key="qualquer").status_code == 401


class TestApiKeyGlobalEHealthz:
    def test_healthz_continua_publico(self):
        from app.servidor import app
        assert TestClient(app).get("/api/healthz").status_code == 200
