"""Camada de dados: detecções, retenção e o comportamento da conexão."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.core import banco


def _ts(dias_atras: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=dias_atras)).isoformat()


class TestContagemPorPlaca:
    def test_conta_todas_ignorando_o_limite_da_listagem(self, ambiente):
        """Regressão: a contagem vinha de uma busca LIKE limitada a 50, então o total
        saturava em 50 por mais detecções que a placa tivesse."""
        for _ in range(60):
            banco.registrar_deteccao("ABC1D23", "mercosul", 0.9)
        assert banco.contar_deteccoes_placa("ABC1D23") == 60

    def test_nao_conta_placa_parecida(self, ambiente):
        banco.registrar_deteccao("ABC1D23", "mercosul", 0.9)
        banco.registrar_deteccao("ABC1D24", "mercosul", 0.9)
        assert banco.contar_deteccoes_placa("ABC1D23") == 1

    def test_ignora_leituras_de_teste(self, ambiente):
        banco.registrar_deteccao("ABC1D23", "mercosul", 0.9, origem="roteador")
        banco.registrar_deteccao("ABC1D23", "mercosul", 0.9, origem="teste")
        assert banco.contar_deteccoes_placa("ABC1D23") == 1
        assert banco.contar_deteccoes_placa("ABC1D23", incluir_testes=True) == 2

    def test_busca_exata_nao_traz_placa_que_apenas_contem(self, ambiente):
        banco.registrar_deteccao("ABC1D23", "mercosul", 0.9)
        banco.registrar_deteccao("XABC1D23Y", "mercosul", 0.9)
        exatas = banco.listar_deteccoes(placa="ABC1D23", placa_exata=True)
        assert [d["placa"] for d in exatas] == ["ABC1D23"]

    def test_busca_parcial_continua_valendo_para_a_interface(self, ambiente):
        banco.registrar_deteccao("ABC1D23", "mercosul", 0.9)
        assert len(banco.listar_deteccoes(placa="BC1")) == 1


class TestConsultaDePlacaPelaApi:
    def test_total_reflete_todas_as_deteccoes(self, admin, ambiente):
        for _ in range(60):
            banco.registrar_deteccao("ABC1D23", "mercosul", 0.9)
        dados = admin.get("/api/placa/ABC1D23").json()
        assert dados["total_deteccoes"] == 60
        assert len(dados["historico"]) <= 9      # o histórico segue recortado
        assert dados["ultima_deteccao"]["placa"] == "ABC1D23"

    def test_placa_sem_deteccao(self, admin):
        dados = admin.get("/api/placa/ZZZ9Z99").json()
        assert dados["total_deteccoes"] == 0
        assert dados["ultima_deteccao"] is None


class TestListagemDeDeteccoes:
    def test_esconde_testes_por_padrao(self, ambiente):
        banco.registrar_deteccao("AAA1A11", "mercosul", 0.9, origem="teste")
        banco.registrar_deteccao("BBB2B22", "mercosul", 0.9, origem="roteador")
        assert [d["placa"] for d in banco.listar_deteccoes()] == ["BBB2B22"]
        assert len(banco.listar_deteccoes(incluir_testes=True)) == 2

    def test_ordena_da_mais_recente_para_a_mais_antiga(self, ambiente):
        banco.registrar_deteccao("AAA1A11", "mercosul", 0.9)
        banco.registrar_deteccao("BBB2B22", "mercosul", 0.9)
        assert [d["placa"] for d in banco.listar_deteccoes()][0] == "BBB2B22"

    def test_filtra_por_bico(self, ambiente):
        banco.registrar_deteccao("AAA1A11", "mercosul", 0.9, bico_id=1)
        banco.registrar_deteccao("BBB2B22", "mercosul", 0.9, bico_id=2)
        assert len(banco.listar_deteccoes(bico_id=1)) == 1


class TestFiltroDeOrigem:
    """O filtro tri-estado que substituiu o booleano `incluir_testes`."""

    def _cenario(self):
        banco.registrar_deteccao("AAA1A11", "mercosul", 0.9, origem="teste")
        banco.registrar_deteccao("BBB2B22", "mercosul", 0.9, origem="roteador")
        banco.registrar_deteccao("CCC3C33", "mercosul", 0.9, origem="pipeline")

    def test_producao_e_o_padrao_e_exclui_so_os_testes(self, ambiente):
        """'produção' não é sinônimo de 'roteador': a detecção do pipeline contínuo
        aconteceu de verdade e precisa continuar aparecendo."""
        self._cenario()
        placas = {d["placa"] for d in banco.listar_deteccoes(origem="producao")}
        assert placas == {"BBB2B22", "CCC3C33"}
        assert placas == {d["placa"] for d in banco.listar_deteccoes()}

    def test_teste_traz_somente_as_leituras_manuais(self, ambiente):
        """O que o booleano antigo não conseguia expressar."""
        self._cenario()
        assert [d["placa"] for d in banco.listar_deteccoes(origem="teste")] == ["AAA1A11"]

    def test_todas_nao_filtra_nada(self, ambiente):
        self._cenario()
        assert len(banco.listar_deteccoes(origem="todas")) == 3

    def test_origem_tem_precedencia_sobre_o_parametro_antigo(self, ambiente):
        self._cenario()
        listadas = banco.listar_deteccoes(incluir_testes=True, origem="teste")
        assert [d["placa"] for d in listadas] == ["AAA1A11"]

    def test_valor_invalido_falha_alto(self, ambiente):
        """Silenciosamente virar 'todas' vazaria testes para um relatório de cobrança."""
        with pytest.raises(ValueError):
            banco.listar_deteccoes(origem="roteador")   # valor da coluna, não do filtro

    def test_contagem_por_placa_aceita_o_mesmo_filtro(self, ambiente):
        banco.registrar_deteccao("ABC1D23", "mercosul", 0.9, origem="roteador")
        banco.registrar_deteccao("ABC1D23", "mercosul", 0.9, origem="teste")
        assert banco.contar_deteccoes_placa("ABC1D23", origem="teste") == 1
        assert banco.contar_deteccoes_placa("ABC1D23", origem="todas") == 2


class TestRetencao:
    def test_apaga_o_que_passou_do_prazo_e_devolve_os_arquivos(self, ambiente):
        antiga = banco.registrar_deteccao("AAA1A11", "mercosul", 0.9,
                                          snapshot="/static/snapshots/velha.jpg")
        with banco.cursor() as c:
            c.execute("UPDATE deteccoes SET criado_em=? WHERE id=?", (_ts(100), antiga))
        banco.registrar_deteccao("BBB2B22", "mercosul", 0.9)

        resultado = banco.deteccoes_e_chamadas_antigas(dias=90)
        assert resultado["deteccoes_removidas"] == 1
        assert "/static/snapshots/velha.jpg" in resultado["arquivos"]
        assert [d["placa"] for d in banco.listar_deteccoes()] == ["BBB2B22"]

    def test_nao_apaga_nada_dentro_do_prazo(self, ambiente):
        banco.registrar_deteccao("AAA1A11", "mercosul", 0.9)
        assert banco.deteccoes_e_chamadas_antigas(dias=90)["deteccoes_removidas"] == 0

    def test_nao_apaga_o_cache_de_veiculos(self, ambiente):
        """A tabela `veiculos` fica FORA da purga, e isso é decisão, não esquecimento.

        Ela é cache de consulta paga: purgá-la reintroduz exatamente o custo que ela
        existe para eliminar — a placa voltaria a ser cobrada no abastecimento seguinte.
        Não há imagem nem vínculo com abastecimento ali, e os dados são do veículo
        (marca, modelo, município), não do proprietário.
        """
        banco.veiculos_salvar("AAA1A11", status="ok",
                              campos={"combustivel": "Alcool / Gasolina"})
        with banco.cursor() as c:
            c.execute("UPDATE veiculos SET consultado_em=? WHERE placa=?",
                      (_ts(2000), "AAA1A11"))

        banco.deteccoes_e_chamadas_antigas(dias=1)

        assert banco.veiculos_obter("AAA1A11") is not None


class TestEsquemaVeiculos:
    def test_tabela_sobrevive_a_reinicializacao(self, ambiente):
        """Mesmo contrato do teste de idempotência acima: `inicializar()` roda em todo
        boot, e um `CREATE TABLE` que não seja idempotente derrubaria o servidor na
        segunda subida — ou, pior, apagaria o cache já pago."""
        banco.veiculos_salvar("AAA1A11", status="ok", campos={"marca": "VW"})
        banco.inicializar()
        banco.inicializar()

        with banco.cursor() as cur:
            cols = {r[1] for r in cur.execute("PRAGMA table_info(veiculos)").fetchall()}
        assert {"placa", "status", "consultado_em", "consultas", "combustivel",
                "combustivel_sigla", "bruto"} <= cols
        assert banco.veiculos_obter("AAA1A11")["marca"] == "VW"


class TestConexao:
    def test_reusa_a_mesma_conexao_na_mesma_thread(self, ambiente):
        assert banco.conexao() is banco.conexao()

    def test_cada_thread_tem_a_sua(self, ambiente):
        """Uma conexão sqlite3 não pode cruzar threads — as threads de câmera e o
        worker de retenção precisam cada um da sua."""
        principal = banco.conexao()
        de_outra: list = []

        def _trabalho():
            de_outra.append(banco.conexao())
            banco.fechar_conexao()

        t = threading.Thread(target=_trabalho)
        t.start()
        t.join()
        assert de_outra[0] is not principal

    def test_erro_faz_rollback_e_nao_contamina_a_proxima_operacao(self, ambiente):
        """A conexão é reusada: sem o rollback, a escrita de uma transação que falhou
        ficaria pendurada e entraria no commit seguinte da mesma thread."""
        try:
            with banco.cursor() as c:
                c.execute("INSERT INTO listas_placas (placa, tipo, criado_em) VALUES (?,?,?)",
                          ("AAA1A11", "branca", _ts()))
                raise RuntimeError("falha no meio da transação")
        except RuntimeError:
            pass

        banco.listas_inserir("BBB2B22", "negra")
        placas = {l["placa"] for l in banco.listas_listar()}
        assert placas == {"BBB2B22"}, "a escrita revertida não pode ter sobrevivido"


class TestListas:
    def test_placa_duplicada_e_rejeitada(self, ambiente):
        import sqlite3
        banco.listas_inserir("ABC1D23", "branca")
        try:
            banco.listas_inserir("ABC1D23", "negra")
            assert False, "deveria violar o UNIQUE"
        except sqlite3.IntegrityError:
            pass

    def test_filtra_por_tipo(self, ambiente):
        banco.listas_inserir("AAA1A11", "branca")
        banco.listas_inserir("BBB2B22", "negra")
        assert len(banco.listas_listar(tipo="negra")) == 1


class TestAcordoEConfirmacao:
    """Uma leitura devolvida por timeout, sem consenso, não pode ficar indistinguível
    de uma leitura sólida — é ela que vira cobrança no cliente errado."""

    def _linha(self, id_):
        import sqlite3
        from app.core.banco import _base
        con = sqlite3.connect(_base.caminho())
        con.row_factory = sqlite3.Row
        try:
            return dict(con.execute("SELECT * FROM deteccoes WHERE id=?", (id_,)).fetchone())
        finally:
            con.close()

    def test_grava_acordo_e_confirmacao(self, ambiente):
        id_ = banco.registrar_deteccao("ABC1D23", "mercosul", 0.9, acordo=0.95, confirmada=True)
        linha = self._linha(id_)
        assert linha["acordo"] == 0.95
        assert linha["confirmada"] == 1

    def test_leitura_fraca_fica_marcada_como_nao_confirmada(self, ambiente):
        id_ = banco.registrar_deteccao("ABC1D23", "mercosul", 0.9, acordo=0.30, confirmada=False)
        assert self._linha(id_)["confirmada"] == 0

    def test_sem_consenso_conhecido_nao_presume_confirmada(self, ambiente):
        """Pipeline ao vivo não passa pelo loop de consenso: NULL, nunca 1."""
        id_ = banco.registrar_deteccao("ABC1D23", "mercosul", 0.9)
        linha = self._linha(id_)
        assert linha["acordo"] is None
        assert linha["confirmada"] is None

    def test_mesclar_leitura_atualiza_o_veredito(self, ambiente):
        """Ao mesclar com a detecção anterior do mesmo bico, o veredito tem de acompanhar
        a leitura nova — senão uma leitura fraca herda o 'confirmada' da anterior."""
        id_ = banco.registrar_deteccao("ABC1D23", "mercosul", 0.9, acordo=0.95, confirmada=True)
        banco.atualizar_deteccao(id_, placa="ABC1D23", padrao="mercosul", confianca=0.8,
                                 acordo=0.40, confirmada=False)
        linha = self._linha(id_)
        assert linha["confirmada"] == 0
        assert linha["acordo"] == 0.40


class TestMigracaoDeEsquema:
    """`_migrar` roda em TODA inicialização (`banco.inicializar()`), inclusive quando o
    processo reinicia contra um banco JÁ migrado — não existia, até aqui, nenhum teste
    cobrindo esse caminho: a suíte só exercita 'banco novo' (a fixture `ambiente` chama
    `inicializar()` uma vez, num `tmp_path` vazio). Um `ALTER TABLE ADD COLUMN` sem o
    guarda `if "x" not in cols` quebraria com "duplicate column name" só em produção, na
    segunda inicialização — nunca na suíte.
    """

    def test_reinicializar_e_idempotente_e_mantem_as_colunas(self, ambiente):
        # `ambiente` já rodou `banco.inicializar()` uma vez ao montar o banco de teste.
        # Rodar de novo, contra o MESMO arquivo já migrado, não pode levantar.
        banco.inicializar()
        banco.inicializar()

        import sqlite3
        from app.core.banco import _base
        con = sqlite3.connect(_base.caminho())
        try:
            cols = {row[1] for row in con.execute("PRAGMA table_info(deteccoes)")}
        finally:
            con.close()
        assert {"tipo_veiculo", "veiculo_classe", "veiculo_conf",
                "tipo_veiculo_fonte", "acordo", "confirmada"} <= cols
