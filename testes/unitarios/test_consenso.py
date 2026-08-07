"""Consenso entre leituras — o que transforma N fotos ruidosas numa placa só.

É a lógica de negócio mais sutil do sistema (vota caractere a caractere, ponderado por
confiança, com prior de formato) e a que mais se paga em teste: um erro aqui não quebra
nada visivelmente, só devolve a placa errada de vez em quando.
"""
from __future__ import annotations

from app.visao.leitura import _consenso_caractere, _eleger_placa, _mesclar_com_anterior


def _candidato(placa, confianca=0.9, padrao="mercosul", detalhes=None):
    return {"placa": placa, "padrao": padrao, "confianca": confianca,
            "detalhes_ocr": detalhes or []}


class TestConsensoCaractere:
    def test_maioria_corrige_um_caractere_isolado(self):
        leituras = [("ABC1D23", 1.0), ("ABC1D23", 1.0), ("ABC1O23", 1.0)]
        assert _consenso_caractere(leituras) == "ABC1D23"

    def test_confianca_pesa_mais_que_quantidade(self):
        """Duas leituras fracas não derrubam uma muito confiante."""
        leituras = [("ABC1D23", 5.0), ("ABC1O23", 0.5), ("ABC1O23", 0.5)]
        assert _consenso_caractere(leituras) == "ABC1D23"

    def test_prior_de_formato_descarta_digito_onde_mercosul_exige_letra(self):
        """Posição 5 do Mercosul é LETRA: mesmo com o dígito em maioria, o voto de
        letra vindo de outro frame é que vale — sem isto o resultado seria 'ABC1223'."""
        leituras = [("ABC1223", 1.0), ("ABC1223", 1.0), ("ABC1Z23", 1.0)]
        assert _consenso_caractere(leituras, formato="mercosul") == "ABC1Z23"

    def test_sem_voto_do_tipo_certo_cai_para_o_voto_bruto(self):
        leituras = [("ABC1223", 1.0), ("ABC1223", 1.0)]
        assert _consenso_caractere(leituras, formato="mercosul") == "ABC1223"

    def test_ignora_leituras_que_nao_tem_7_caracteres(self):
        leituras = [("ABC1D23", 1.0), ("ABC12", 1.0), ("", 1.0)]
        assert _consenso_caractere(leituras) == "ABC1D23"

    def test_sem_leitura_valida_devolve_none(self):
        assert _consenso_caractere([("ABC12", 1.0)]) is None
        assert _consenso_caractere([]) is None


class TestElegerPlaca:
    def test_sem_candidatos_devolve_none(self):
        assert _eleger_placa([]) is None

    def test_elege_por_consenso_e_nao_pela_string_mais_votada(self):
        eleito = _eleger_placa([
            _candidato("ABC1D23"), _candidato("ABC1D23"), _candidato("ABC1O23"),
        ])
        assert eleito["placa"] == "ABC1D23"
        assert eleito["n_votos_snap"] == 2

    def test_acordo_total_quando_todos_concordam(self):
        eleito = _eleger_placa([_candidato("ABC1D23"), _candidato("ABC1D23")])
        assert eleito["acordo"] == 1.0

    def test_acordo_cai_quando_as_leituras_divergem(self):
        eleito = _eleger_placa([_candidato("ABC1D23"), _candidato("XYZ9K88")])
        assert eleito["acordo"] < 1.0

    def test_confianca_final_e_escalada_pelo_acordo(self):
        """Discordância tem que reduzir a confiança reportada — é ela que o roteador vê."""
        junto = _eleger_placa([_candidato("ABC1D23", 0.9), _candidato("ABC1D23", 0.9)])
        brigado = _eleger_placa([_candidato("ABC1D23", 0.9), _candidato("XYZ9K88", 0.9)])
        assert brigado["confianca"] < junto["confianca"]

    def test_padrao_e_recalculado_a_partir_da_placa_eleita(self):
        eleito = _eleger_placa([_candidato("ABC1234", padrao="mercosul")])
        assert eleito["padrao"] == "antigo"

    def test_engines_individuais_entram_na_votacao(self):
        """`detalhes_ocr` são as leituras de cada engine — o voto delas conta."""
        eleito = _eleger_placa([
            _candidato("ABC1O23", 0.5, detalhes=[
                {"placa": "ABC1D23", "confianca": 0.9},
                {"placa": "ABC1D23", "confianca": 0.9},
            ]),
        ])
        assert eleito["placa"] == "ABC1D23"


class TestMesclarComAnterior:
    def test_ruido_de_um_caractere_converge_para_a_mesma_placa(self):
        atual = {"placa": "ABC1D23", "confianca": 0.7, "padrao": "mercosul"}
        anterior = {"placa": "ABC1D23", "confianca": 0.9}
        assert _mesclar_com_anterior(atual, anterior)["placa"] == "ABC1D23"

    def test_mantem_a_maior_confianca_das_duas(self):
        atual = {"placa": "ABC1D23", "confianca": 0.7, "padrao": "mercosul"}
        anterior = {"placa": "ABC1D23", "confianca": 0.95}
        assert _mesclar_com_anterior(atual, anterior)["confianca"] == 0.95

    def test_sem_consenso_valido_fica_com_a_leitura_mais_confiante(self):
        atual = {"placa": "ABC1D23", "confianca": 0.2, "padrao": "mercosul"}
        anterior = {"placa": "XYZ9K88", "confianca": 0.9}
        assert _mesclar_com_anterior(atual, anterior)["placa"] == "XYZ9K88"
