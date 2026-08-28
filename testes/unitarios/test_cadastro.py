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


class TestBicoComDuasCameras:
    """Um bico pode enxergar o veículo por 2 câmeras (traseira + frente).

    A regra central é DEGRADAR: perder uma câmera não pode derrubar a leitura, senão a
    segunda câmera — que existe para ser rede de segurança — vira um ponto de falha novo.
    """

    def test_resolve_as_duas_cameras_com_seus_papeis(self, posto_2cam):
        reg, motivo = banco.resolver_bico(posto_2cam["cnpj"], "1", "3")
        assert motivo is None
        assert [c["camera_id"] for c in reg["cameras"]] == [posto_2cam["camera_id"],
                                                            posto_2cam["camera2_id"]]
        assert [c["papel"] for c in reg["cameras"]] == ["traseira", "frente"]
        # Formato achatado intacto: `EspecificacaoCamera.de_camera_db(reg, cfg)` depende dele
        assert reg["camera_id"] == posto_2cam["camera_id"]
        assert reg["camera_tipo"] == "rtsp"

    def test_secundaria_inativa_degrada_e_avisa(self, posto_2cam):
        with banco.cursor() as c:
            c.execute("UPDATE cameras SET ativo=0 WHERE id=?", (posto_2cam["camera2_id"],))
        reg, motivo = banco.resolver_bico(posto_2cam["cnpj"], "1", "3")
        assert motivo is None                       # a leitura continua possível
        assert [c["camera_id"] for c in reg["cameras"]] == [posto_2cam["camera_id"]]
        assert reg["avisos"], "a câmera desativada tem que ser sinalizada"

    def test_primaria_inativa_promove_a_secundaria(self, posto_2cam):
        """Os campos achatados descrevem a primeira câmera UTILIZÁVEL, não o slot 1 —
        senão quem lê o formato antigo receberia uma câmera que está fora do ar."""
        with banco.cursor() as c:
            c.execute("UPDATE cameras SET ativo=0 WHERE id=?", (posto_2cam["camera_id"],))
        reg, motivo = banco.resolver_bico(posto_2cam["cnpj"], "1", "3")
        assert motivo is None
        assert reg["camera_id"] == posto_2cam["camera2_id"]
        assert [c["papel"] for c in reg["cameras"]] == ["frente"]

    def test_as_duas_inativas_falha_como_camera_inativa(self, posto_2cam):
        with banco.cursor() as c:
            c.execute("UPDATE cameras SET ativo=0")
        reg, motivo = banco.resolver_bico(posto_2cam["cnpj"], "1", "3")
        assert reg is None and motivo == "camera_inativa"

    def test_mesmo_gate_partindo_do_id_do_bico(self, posto_2cam):
        with banco.cursor() as c:
            c.execute("UPDATE cameras SET ativo=0 WHERE id=?", (posto_2cam["camera2_id"],))
        bico, motivo = banco.bico_verificar_ativo(posto_2cam["bico_id"])
        assert motivo is None and bico is not None   # degrada igual à leitura reativa


class TestValidacaoDaSegundaCamera:
    def test_segunda_camera_de_outro_posto_e_recusada(self, admin, posto):
        """Espelho da validação da primeira: deixar a segunda de fora reabriria por outro
        campo exatamente o vazamento entre postos que a primeira fecha."""
        outra_ent = admin.post("/api/entidades", json={"nome": "Rede B"}).json()["id"]
        outra_emp = admin.post("/api/empresas", json={
            "entidade_id": outra_ent, "nome": "Posto B", "cnpj": "45723174000110"}).json()["id"]
        cam_alheia = admin.post("/api/cameras", json={
            "nome": "Cam alheia", "empresa_id": outra_emp,
            "camera_tipo": "rtsp", "rtsp_url_custom": "rtsp://y/1"}).json()["id"]

        r = admin.put(f"/api/bicos/{posto['bico_id']}", json={
            "automacao_id": posto["automacao_id"], "codigo": "3",
            "camera_id": posto["camera_id"], "camera2_id": cam_alheia})
        assert r.status_code == 400
        assert "mesmo posto" in r.json()["detail"]

    def test_segunda_camera_igual_a_primeira_e_recusada(self, admin, posto):
        r = admin.put(f"/api/bicos/{posto['bico_id']}", json={
            "automacao_id": posto["automacao_id"], "codigo": "3",
            "camera_id": posto["camera_id"], "camera2_id": posto["camera_id"]})
        assert r.status_code == 400

    def test_papel_invalido_e_recusado(self, admin, posto_2cam):
        r = admin.put(f"/api/bicos/{posto_2cam['bico_id']}", json={
            "automacao_id": posto_2cam["automacao_id"], "codigo": "3",
            "camera_id": posto_2cam["camera_id"], "camera2_id": posto_2cam["camera2_id"],
            "papel_camera2": "lateral"})
        assert r.status_code == 400

    def test_papeis_iguais_nas_duas_cameras_sao_recusados(self, admin, posto_2cam):
        """O papel e o NOME pelo qual a tela distingue as duas fontes ("frente nao detectou
        placa", quadro realcado no teste). Com as duas chamadas "traseira" o diagnostico de
        duas fontes para de responder a unica pergunta que ele existe para responder: em
        qual das cameras mexer."""
        r = admin.put(f"/api/bicos/{posto_2cam['bico_id']}", json={
            "automacao_id": posto_2cam["automacao_id"], "codigo": "3",
            "camera_id": posto_2cam["camera_id"], "camera2_id": posto_2cam["camera2_id"],
            "papel_camera": "traseira", "papel_camera2": "traseira"})
        assert r.status_code == 400
        assert "mesmo lado" in r.json()["detail"]

    def test_papel_repetido_sem_segunda_camera_nao_barra(self, admin, posto):
        """Bico de uma camera: `papel_camera2` fica no default do banco e nao descreve
        fonte nenhuma. Barrar aqui recusaria cadastro correto por causa de um campo que a
        tela nem mostra nesse caso."""
        r = admin.put(f"/api/bicos/{posto['bico_id']}", json={
            "automacao_id": posto["automacao_id"], "codigo": "3",
            "camera_id": posto["camera_id"], "camera2_id": None,
            "papel_camera": "frente", "papel_camera2": "frente"})
        assert r.status_code == 200

    def test_camera_usada_so_como_secundaria_nao_pode_ser_removida(self, admin, posto_2cam):
        """Sem casar os dois slots na consulta por câmera, apagar uma câmera usada apenas
        como segunda passaria pelo guard e quebraria o bico."""
        r = admin.delete(f"/api/cameras/{posto_2cam['camera2_id']}")
        assert r.status_code == 409
        assert banco.cameras_obter(posto_2cam["camera2_id"]) is not None


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


class TestTelaDeBicosAposentada:
    """A tela avulsa /bicos saiu: ela cadastrava o mesmo bico que o modal da tela do posto,
    mas sem barrar câmera repetida nos dois slots e sem avisar que trocar a segunda câmera
    apaga a área desenhada nela. Estes testes travam o redirecionamento — sem eles, um link
    esquecido em outro template só apareceria como erro de template em produção.
    """

    def test_sem_parametro_vai_para_a_lista_de_postos(self, admin):
        r = admin.get("/bicos", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/postos"

    def test_com_automacao_cai_no_posto_daquela_automacao(self, admin, posto):
        """O link que existia em /automacoes passava `automacao_id`; jogar todo mundo em
        /postos perderia o contexto que a pessoa já tinha escolhido."""
        r = admin.get(f"/bicos?automacao_id={posto['automacao_id']}", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == f"/posto/{posto['empresa_id']}"

    def test_cliente_nao_recebe_o_destino_contextual(self, cliente_logado, posto):
        """`automacao_id` e um inteiro sequencial: devolver /posto/{empresa_id} para um
        usuario 'cliente' revelaria, iterando o parametro e lendo o Location, a que posto
        cada automacao pertence. A pagina que existia aqui era admin-only exatamente por
        isso. Cliente vai para /postos, que ja mostra so o posto dele.
        """
        r = cliente_logado.get(f"/bicos?automacao_id={posto['automacao_id']}",
                               follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/postos"

    def test_automacao_inexistente_nao_estoura(self, admin):
        r = admin.get("/bicos?automacao_id=99999", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/postos"
