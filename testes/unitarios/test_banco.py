"""Camada de dados: detecções, retenção e o comportamento da conexão."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

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
