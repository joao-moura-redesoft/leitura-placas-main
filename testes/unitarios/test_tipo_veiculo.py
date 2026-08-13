"""Filtro de tipo de veículo (moto/carro) no histórico de leituras.

O valor é uma ESTIMATIVA do AutoOCR (`e_moto`, hoje `header and aspect <= 2.0`), não um
cadastro. O que estes testes protegem não é a acurácia dela — é que o "não sei" continue
distinguível do "é carro": fundir os dois faria o histórico afirmar, sobre centenas de
leituras antigas, um tipo que ninguém mediu.
"""
from __future__ import annotations

import pytest

from app.core import banco
from app.core.banco._deteccoes import _filtro_tipo_veiculo


class TestFiltroSQL:
    def test_todos_e_none_nao_filtram(self):
        assert _filtro_tipo_veiculo(None) == ""
        assert _filtro_tipo_veiculo("todos") == ""

    def test_desconhecido_vira_is_null_sem_parametro(self):
        """'desconhecido' não pode virar `= ?`: em SQL, `tipo_veiculo = NULL` nunca é
        verdadeiro e o filtro devolveria zero linhas em vez das não estimadas."""
        frag = _filtro_tipo_veiculo("desconhecido")
        assert "IS NULL" in frag and "?" not in frag

    def test_moto_e_carro_usam_parametro(self):
        for t in ("moto", "carro"):
            assert "?" in _filtro_tipo_veiculo(t)

    def test_valor_invalido_levanta(self):
        """Espelha `_filtro_origem`: falhar alto, em vez de devolver o conjunto errado."""
        with pytest.raises(ValueError, match="tipo_veiculo"):
            _filtro_tipo_veiculo("caminhao")


class TestGravacaoEFiltro:
    def _gravar(self, ambiente):
        ids = {
            "moto": banco.registrar_deteccao("ABC1D23", "mercosul", 0.9, tipo_veiculo="moto"),
            "carro": banco.registrar_deteccao("XYZ9K88", "mercosul", 0.9, tipo_veiculo="carro"),
            "nulo": banco.registrar_deteccao("HPY2371", "antigo", 0.9),
        }
        return ids

    def test_grava_e_devolve_o_tipo(self, ambiente):
        self._gravar(ambiente)
        por_placa = {d["placa"]: d for d in banco.listar_deteccoes(limit=50)}
        assert por_placa["ABC1D23"]["tipo_veiculo"] == "moto"
        assert por_placa["XYZ9K88"]["tipo_veiculo"] == "carro"
        assert por_placa["HPY2371"]["tipo_veiculo"] is None

    def test_omitir_o_tipo_grava_null_e_nao_carro(self, ambiente):
        """Default 'carro' seria inventar dado: leitura por engine único não estima."""
        banco.registrar_deteccao("HPY2371", "antigo", 0.9)
        assert banco.listar_deteccoes(limit=1)[0]["tipo_veiculo"] is None

    @pytest.mark.parametrize("tipo,esperadas", [
        ("moto", {"ABC1D23"}),
        ("carro", {"XYZ9K88"}),
        ("desconhecido", {"HPY2371"}),
        ("todos", {"ABC1D23", "XYZ9K88", "HPY2371"}),
        (None, {"ABC1D23", "XYZ9K88", "HPY2371"}),
    ])
    def test_cada_filtro_traz_o_seu_conjunto(self, ambiente, tipo, esperadas):
        self._gravar(ambiente)
        achadas = {d["placa"] for d in banco.listar_deteccoes(limit=50, tipo_veiculo=tipo)}
        assert achadas == esperadas

    def test_linhas_sem_estimativa_nao_somem_de_todos(self, ambiente):
        """Regressão do padrão que já mordeu o filtro de origem: um NOT IN faria as
        linhas NULL desaparecerem de TODOS os filtros, inclusive do "Todos"."""
        self._gravar(ambiente)
        todas = banco.listar_deteccoes(limit=50, tipo_veiculo="todos")
        assert any(d["tipo_veiculo"] is None for d in todas)

    def test_tipo_invalido_na_gravacao_levanta(self, ambiente):
        with pytest.raises(ValueError, match="tipo_veiculo"):
            banco.registrar_deteccao("ABC1D23", "mercosul", 0.9, tipo_veiculo="caminhao")

    def test_contagem_por_placa_respeita_o_filtro(self, ambiente):
        """O total do cabeçalho e a lista precisam concordar — senão a mesma tela
        mostra dois números que se contradizem."""
        banco.registrar_deteccao("ABC1D23", "mercosul", 0.9, tipo_veiculo="moto")
        banco.registrar_deteccao("ABC1D23", "mercosul", 0.9, tipo_veiculo="carro")
        assert banco.contar_deteccoes_placa("ABC1D23") == 2
        assert banco.contar_deteccoes_placa("ABC1D23", tipo_veiculo="moto") == 1
        assert banco.contar_deteccoes_placa("ABC1D23", tipo_veiculo="desconhecido") == 0

    def test_mesclagem_nao_apaga_o_tipo_ja_gravado(self, ambiente):
        """`atualizar_deteccao` roda quando o roteador repete a chamada do mesmo
        veículo. A leitura que mescla pode não ter estimativa; sobrescrever com NULL
        apagaria a que já estava lá."""
        id_ = banco.registrar_deteccao("ABC1D23", "mercosul", 0.9, tipo_veiculo="moto")
        banco.atualizar_deteccao(id_, placa="ABC1D23", padrao="mercosul", confianca=0.95)
        assert banco.listar_deteccoes(limit=1)[0]["tipo_veiculo"] == "moto"

    def test_mesclagem_preenche_o_tipo_que_faltava(self, ambiente):
        id_ = banco.registrar_deteccao("ABC1D23", "mercosul", 0.9)
        banco.atualizar_deteccao(id_, placa="ABC1D23", padrao="mercosul", confianca=0.95,
                                 tipo_veiculo="moto")
        assert banco.listar_deteccoes(limit=1)[0]["tipo_veiculo"] == "moto"


class TestEstimativaDoOCR:
    """A tradução de `e_moto`/`tinha_header` para 'moto'/'carro'/None."""

    @pytest.mark.parametrize("e_moto,tinha_header,esperado", [
        (True, True, "moto"),
        (False, True, "carro"),
        # Sem header não há como afirmar o tipo: uma placa ANTIGA de moto não tem a faixa
        # azul e cairia como 'carro' se o False de `e_moto` fosse lido como carro.
        (False, False, None),
    ])
    def test_tres_estados_e_nao_dois(self, e_moto, tinha_header, esperado):
        tipo = "moto" if e_moto else ("carro" if tinha_header else None)
        assert tipo == esperado
