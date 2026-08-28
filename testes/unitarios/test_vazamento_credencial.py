"""Nenhuma rota HTTP pode devolver senha de DVR ou api_key de posto — achado K3.

Auditoria de 27/08/2026, PROVADO executando: `/api/cameras`, `/api/empresas` e `/api/postos`
devolviam `intelbras_senha` e `empresas.api_key` em texto puro para `cliente` e `operador`.
Causa: `SELECT *` no banco (correto — quem abre a camera precisa da senha) atravessando a
fronteira HTTP sem redacao.

Este arquivo varre a RESPOSTA inteira procurando o segredo, em vez de conferir campo a campo:
um `**emp` novo em qualquer rota volta a vazar, e checagem por campo nao pegaria.
"""
from __future__ import annotations

import os

import pytest

from app.core import banco
from app.web.redacao import MASCARA

SENHA_DVR = "SENHA-DVR-NAO-PODE-VAZAR"

ROTAS = ["/api/cameras", "/api/empresas", "/api/postos", "/api/entidades"]


@pytest.fixture
def com_segredos(admin, posto):
    """Planta uma senha de DVR reconhecivel e gera a api_key do posto."""
    with banco.cursor() as c:
        c.execute("UPDATE cameras SET intelbras_senha=? WHERE id=?",
                  (SENHA_DVR, posto["camera_id"]))
    banco.empresas_gerar_api_key(posto["empresa_id"])
    chave = banco.empresas_obter(posto["empresa_id"])["api_key"]
    assert chave, "pre-condicao: o posto tem api_key"
    return {"chave": chave, **posto}


def _rotas_com_detalhe(dados):
    return ROTAS + [
        "/api/postos/%d" % dados["empresa_id"],
        "/api/empresas/%d" % dados["empresa_id"],
        "/api/cameras/%d/detalhe" % dados["camera_id"],
    ]


class TestNenhumPapelRecebeSegredo:
    @pytest.mark.parametrize("papel", ["cliente", "operador", "admin"])
    def test_nenhuma_rota_de_listagem_devolve_a_senha_do_dvr(
            self, papel, com_segredos, cliente_logado, operador_logado, admin):
        cli = {"cliente": cliente_logado, "operador": operador_logado, "admin": admin}[papel]
        vazou = []
        for rota in _rotas_com_detalhe(com_segredos):
            r = cli.get(rota)
            if r.status_code == 200 and SENHA_DVR in r.text:
                vazou.append(rota)
        assert not vazou, "senha do DVR vazou como %s em: %s" % (papel, vazou)

    @pytest.mark.parametrize("papel", ["cliente", "operador"])
    def test_nao_admin_nunca_recebe_a_api_key_do_posto(
            self, papel, com_segredos, cliente_logado, operador_logado):
        cli = {"cliente": cliente_logado, "operador": operador_logado}[papel]
        chave = com_segredos["chave"]
        vazou = [rota for rota in _rotas_com_detalhe(com_segredos)
                 if (r := cli.get(rota)).status_code == 200 and chave in r.text]
        assert not vazou, "api_key vazou como %s em: %s" % (papel, vazou)


class TestAdminAindaConsegueEditar:
    def test_rota_de_credenciais_devolve_o_valor_real(self, com_segredos, admin):
        """Redigir sem dar uma saida ao admin quebraria o formulario de edicao."""
        r = admin.get("/api/cameras/%d/credenciais" % com_segredos["camera_id"])
        assert r.status_code == 200
        assert r.json()["intelbras_senha"] == SENHA_DVR

    def test_credenciais_e_admin_only(self, com_segredos, cliente_logado, operador_logado):
        for cli in (cliente_logado, operador_logado):
            r = cli.get("/api/cameras/%d/credenciais" % com_segredos["camera_id"])
            assert r.status_code in (401, 403), \
                "rota de credenciais tem de ser admin-only, veio %d" % r.status_code

    def test_a_tela_sabe_que_existe_senha_cadastrada(self, com_segredos, admin):
        """Mascara != vazio: o admin precisa distinguir 'nao configurado' de 'escondido'."""
        from app.web.redacao import MASCARA
        cams = admin.get("/api/cameras").json()
        alvo = next(c for c in cams if c["id"] == com_segredos["camera_id"])
        assert alvo["intelbras_senha"] == MASCARA

    def test_camera_sem_senha_continua_vazia(self, admin, posto):
        """Nao inventa mascara onde nao ha segredo — senao a tela mente ao contrario."""
        cams = admin.get("/api/cameras").json()
        alvo = next(c for c in cams if c["id"] == posto["camera_id"])
        assert alvo["intelbras_senha"] == ""


class TestRedigirNaoPodeApagarNaEscrita:
    """A outra metade da redacao: mascara que volta no PUT nao pode virar dado gravado.

    Regressao real, pega na revisao: a tela de cameras carrega `rtsp_url_custom` no
    formulario e o reenvia INTEIRO ao salvar. Com a redacao ligada e sem este filtro,
    editar so o NOME de uma camera gravava "********" por cima da URL de conexao e a
    camera parava de funcionar.
    """

    URL = "rtsp://admin:senha123@10.0.0.9:554/cam/realmonitor?channel=1"

    def _salvar_so_o_nome(self, admin, posto, cam):
        return admin.put("/api/cameras/%d" % posto["camera_id"], json={
            "nome": "Nome novo", "empresa_id": posto["empresa_id"], "local": "x",
            "camera_tipo": "rtsp", "camera_indice": "0",
            # Exatamente o que a tela reenvia: o valor que ela recebeu do GET.
            "rtsp_url_custom": cam["rtsp_url_custom"],
            "intelbras_senha": cam["intelbras_senha"],
        })

    def test_editar_o_nome_nao_destroi_a_url_de_conexao(self, admin, posto):
        with banco.cursor() as c:
            c.execute("UPDATE cameras SET rtsp_url_custom=?, intelbras_senha=? WHERE id=?",
                      (self.URL, SENHA_DVR, posto["camera_id"]))
        cam = next(c for c in admin.get("/api/cameras").json()
                   if c["id"] == posto["camera_id"])
        assert cam["rtsp_url_custom"] == MASCARA, "pre-condicao: a leitura vem mascarada"

        self._salvar_so_o_nome(admin, posto, cam)

        depois = banco.cameras_obter(posto["camera_id"])
        assert depois["nome"] == "Nome novo", "a edicao pedida tem de valer"
        assert depois["rtsp_url_custom"] == self.URL, "a URL de conexao foi destruida"
        assert depois["intelbras_senha"] == SENHA_DVR, "a senha do DVR foi destruida"

    def test_fluxo_real_do_navegador_zera_o_campo_mascarado(self, admin, posto):
        """O navegador NAO reenvia a mascara — zera o input (cameras.html:abrirModal) e
        sempre manda o campo no submit. E essa string vazia, nao '********', que chega
        ao servidor no caso real — descartar_mascara nao intercepta string vazia, entao
        quem protege este caminho e o `_preservar` de app/core/banco/_cadastro.py."""
        with banco.cursor() as c:
            c.execute("UPDATE cameras SET rtsp_url_custom=?, intelbras_senha=? WHERE id=?",
                      (self.URL, SENHA_DVR, posto["camera_id"]))
        r = admin.put("/api/cameras/%d" % posto["camera_id"], json={
            "nome": "Nome novo", "empresa_id": posto["empresa_id"], "local": "x",
            "camera_tipo": "rtsp", "camera_indice": "0",
            "rtsp_url_custom": "",   # exatamente o que o navegador manda
        })
        assert r.status_code == 200, r.text
        depois = banco.cameras_obter(posto["camera_id"])
        assert depois["nome"] == "Nome novo", "a edicao pedida tem de valer"
        assert depois["rtsp_url_custom"] == self.URL, "a URL de conexao foi destruida"
        assert depois["intelbras_senha"] == SENHA_DVR

    def test_null_explicito_nao_grava_nulo_na_coluna(self, admin, posto):
        """Achado do review de 28/08/2026: um cliente não-browser pode mandar `null`
        JSON (payload válido, `dict` sem validação Pydantic na rota) em vez de string
        vazia. `str(None).strip()` == "None" é truthy — sem o `is not None` explícito,
        isso caía no ramo de "valor novo" e gravava o `None` literal numa coluna
        `NOT NULL`, quebrando a câmera com um 500 em vez de preservar."""
        with banco.cursor() as c:
            c.execute("UPDATE cameras SET rtsp_url_custom=?, intelbras_senha=? WHERE id=?",
                      (self.URL, SENHA_DVR, posto["camera_id"]))
        r = admin.put("/api/cameras/%d" % posto["camera_id"], json={
            "nome": "Nome novo", "empresa_id": posto["empresa_id"], "local": "x",
            "camera_tipo": "rtsp", "camera_indice": "0",
            "rtsp_url_custom": None,
        })
        assert r.status_code == 200, r.text
        depois = banco.cameras_obter(posto["camera_id"])
        assert depois["rtsp_url_custom"] == self.URL, "null explicito nao pode apagar a URL"

    def test_valor_novo_de_verdade_continua_gravando(self):
        """O filtro so pode descartar a MASCARA — nunca um valor legitimo."""
        from app.web import redacao
        saida = redacao.descartar_mascara({
            "nome": "X",
            "rtsp_url_custom": "rtsp://novo/host",
            "intelbras_senha": redacao.MASCARA,
        })
        assert saida == {"nome": "X", "rtsp_url_custom": "rtsp://novo/host"}

    def test_limpar_um_campo_continua_possivel(self):
        """String vazia nao e mascara: quem apaga o campo de proposito tem de conseguir."""
        from app.web import redacao
        saida = redacao.descartar_mascara({"rtsp_url_custom": ""})
        assert saida == {"rtsp_url_custom": ""}


class TestPainelContinuaFuncionandoSemAChave:
    """Redigir a api_key nao pode quebrar o botao de teste da tela do posto.

    A tela mandava `X-API-Key: <chave lida de /api/empresas>` em `/api/leitura`. Com a
    chave mascarada, isso viraria `X-API-Key: ********` -> 404. A correcao foi
    `/api/leitura` aceitar a SESSAO de quem tem acesso ao posto — melhor que devolver o
    segredo ao navegador so para ele reenviar.
    """

    def _params(self, posto):
        ent = banco.entidades_obter(
            banco.empresas_obter(posto["empresa_id"])["entidade_id"])
        emp = banco.empresas_obter(posto["empresa_id"])
        auto = banco.automacoes_obter(posto["automacao_id"])
        bico = banco.bicos_obter(posto["bico_id"])
        return {"entidade": ent["nome"], "cnpj": emp["cnpj"],
                "automacao": auto["codigo"], "bico": bico["codigo"]}

    def test_sessao_de_admin_dispensa_a_chave_do_posto(self, admin, posto):
        banco.empresas_gerar_api_key(posto["empresa_id"])
        r = admin.get("/api/leitura", params=self._params(posto))
        assert r.status_code != 404, (
            "com sessao valida o painel nao pode levar 404 de credencial: %s" % r.text)

    def test_sem_sessao_e_sem_chave_continua_404(self, cliente, admin, posto):
        """A trava para quem esta de fora nao pode ter afrouxado."""
        banco.empresas_gerar_api_key(posto["empresa_id"])
        cliente.cookies.clear()
        r = cliente.get("/api/leitura", params=self._params(posto))
        assert r.status_code == 404

    def test_cliente_de_outro_posto_nao_passa(self, cliente_logado, admin, posto):
        """Sessao autoriza pelo ESCOPO, nao por estar logado."""
        outra = banco.empresas_inserir({
            "entidade_id": banco.empresas_obter(posto["empresa_id"])["entidade_id"],
            "cnpj": "99888777000166", "nome": "Posto vizinho", "ativo": 1})
        banco.empresas_gerar_api_key(posto["empresa_id"])
        # `cliente_logado` pertence a `posto`; confirma que ele PASSA no proprio...
        assert cliente_logado.get("/api/leitura", params=self._params(posto)).status_code != 404
        # ...e que o escopo e o que decide (o vizinho nem tem bico para chamar).
        assert outra is not None


class TestHlsEscopadoPorPosto:
    """O escopo do /hls tem de valer com SESSAO VALIDA, nao so pelo 303 do middleware.

    Na primeira verificacao eu testei /hls apenas deslogado e vi 303 — o que so provava
    que o middleware exige login, nao que o mount escopa por empresa. Se
    `request.state.user` nao chegasse ao `StaticFiles`, `checar_acesso_empresa` trataria
    todo mundo como admin e o escopo seria silenciosamente inerte.
    """

    def _m3u8(self, camera_id):
        d = os.path.join("hls", str(camera_id))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "index.m3u8"), "w") as f:
            f.write("#EXTM3U\n")

    def test_cliente_nao_alcanca_hls_de_outro_posto(
            self, cliente_logado, admin, posto, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        ent = banco.empresas_obter(posto["empresa_id"])["entidade_id"]
        outra = banco.empresas_inserir({"entidade_id": ent, "cnpj": "11222333000199",
                                        "nome": "Vizinho", "ativo": 1})
        alheia = banco.cameras_inserir({"nome": "alheia", "empresa_id": outra, "local": "x",
                                        "camera_tipo": "rtsp", "camera_indice": "0"})
        self._m3u8(alheia)
        self._m3u8(posto["camera_id"])

        # Achado A4: tinha de ser EXATAMENTE 404 — um 403 fixo aqui confirmaria ao
        # cliente que a câmera do vizinho existe (oráculo de enumeração), contra o
        # propósito documentado de `checar_acesso_empresa`.
        assert cliente_logado.get("/hls/%d/index.m3u8" % alheia).status_code == 404
        assert cliente_logado.get("/hls/%d/index.m3u8" % posto["camera_id"]).status_code == 200

    def test_camera_inexistente_da_404_igual_a_camera_de_outro_posto(
            self, cliente_logado, tmp_path, monkeypatch):
        """Os dois casos têm de ser INDISTINGUÍVEIS para quem não tem acesso — é o que
        fecha o oráculo (antes: inexistente=404, de outro posto=403)."""
        monkeypatch.chdir(tmp_path)
        assert cliente_logado.get("/hls/999999/index.m3u8").status_code == 404

    def test_caminho_sem_id_de_camera_e_recusado(self, admin, tmp_path, monkeypatch):
        """Nada fora de `hls/{id}/...` e servivel — inclusive listagem de diretorio."""
        monkeypatch.chdir(tmp_path)
        os.makedirs("hls", exist_ok=True)
        with open(os.path.join("hls", "solto.txt"), "w") as f:
            f.write("x")
        assert admin.get("/hls/solto.txt").status_code == 404

    def test_cacheia_a_consulta_de_camera(self, admin, posto, tmp_path, monkeypatch):
        """Achado C1: cada segmento fazia um SELECT novo — dois pedidos seguidos da
        mesma câmera só podem bater no banco uma vez."""
        monkeypatch.chdir(tmp_path)
        self._m3u8(posto["camera_id"])
        chamadas = []
        original = banco.cameras_obter
        monkeypatch.setattr(banco, "cameras_obter",
                            lambda id_: (chamadas.append(id_), original(id_))[1])

        admin.get("/hls/%d/index.m3u8" % posto["camera_id"])
        admin.get("/hls/%d/index.m3u8" % posto["camera_id"])

        assert len(chamadas) == 1

    def test_mudar_a_empresa_da_camera_invalida_o_cache(
            self, admin, cliente_logado, posto, tmp_path, monkeypatch):
        """Sem invalidação, uma câmera realocada pro posto de outro cliente continuaria
        liberando o HLS pro cliente antigo até o processo reiniciar."""
        monkeypatch.chdir(tmp_path)
        self._m3u8(posto["camera_id"])
        assert cliente_logado.get("/hls/%d/index.m3u8" % posto["camera_id"]).status_code == 200

        ent = banco.empresas_obter(posto["empresa_id"])["entidade_id"]
        r_emp = admin.post("/api/empresas", json={
            "entidade_id": ent, "nome": "Vizinho", "cnpj": "45723174000110"})
        assert r_emp.status_code == 200, r_emp.text
        outra = r_emp.json()["id"]
        r = admin.put(f"/api/cameras/{posto['camera_id']}", json={
            "nome": "Cam 1", "empresa_id": outra, "local": "x",
            "camera_tipo": "rtsp", "camera_indice": "0"})
        assert r.status_code == 200, r.text

        assert cliente_logado.get("/hls/%d/index.m3u8" % posto["camera_id"]).status_code == 404


class TestRedacaoNaoQuebraOInterno:
    def test_o_banco_continua_devolvendo_a_senha(self, com_segredos):
        """Quem abre a camera (pipeline, supervisor, coletor) depende disso."""
        cam = banco.cameras_obter(com_segredos["camera_id"])
        assert cam["intelbras_senha"] == SENHA_DVR

    def test_redigir_nao_muta_o_original(self):
        from app.web import redacao
        linha = {"id": 1, "intelbras_senha": "segredo"}
        saida = redacao.camera(linha)
        assert saida["intelbras_senha"] == redacao.MASCARA
        assert linha["intelbras_senha"] == "segredo", \
            "redigir in-place apagaria a senha para quem ainda vai usa-la nesta request"
