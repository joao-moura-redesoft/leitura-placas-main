"""O que `snapshots_votacao` de fato compra, e o que ele NAO compra.

O laco reject-retry so avalia a parada antecipada depois de `tentativas >= n_min`
(`leitura.py`), e `tentativas` e incrementado assim que um FRAME chega — antes de detectar
ou ler. Ou seja: `n_min` e piso de *frames analisados*, nao de evidencia. Quem barra leitura
fraca sao as outras duas condicoes da mesma linha, `acordo >= leitura_acordo_minimo` e
`n_votos_leitura >= 2`, e essa segunda foi calibrada contra falso positivo do detector
(80 recortes reais x 80 falsos: 86% das reais passam, 4% dos falsos passam — ver
`leitura._eleger_placa`).

Isso importa porque o piso custa tempo de roteador: medido no ALTIPLANO em 31/08/2026, uma
chamada real gastou 22,4 s em 3 fotos (~7 s cada) com a foto 1 ja trazendo os 4 engines
exatos a conf 0,99. Cada frame a mais no piso e ~1/3 da espera do frentista.

Estes testes fixam as duas pontas do trade-off, para o valor de `snapshots_votacao` nao ser
mexido no escuro:

  - com o ensemble REAL (varias leituras por foto), `n_min=1` para na PRIMEIRA foto — o
    ensemble concordando consigo mesmo nao e concordancia entre fotos, que e a evidencia
    que o piso existe para exigir;
  - `n_min=2` para na segunda e e o menor valor que preserva essa concordancia;
  - `n_min=3` (o default de hoje) gasta uma foto a mais sem mudar `confirmada`, porque
    `consenso.confirmada` usa `min(2, n_min)` e satura em 2.
"""
from __future__ import annotations

import logging

import pytest

from app.visao import leitura as leitura_mod
from app.visao.consenso import confirmada
from app.visao.leitura import FonteLeitura

from test_payload_leitura import (CFG, BICO_ID, PLACA, _DetectorFalso, _OcrFalso,
                                  _especificacao, _provedor_de_frames, visao_falsa)  # noqa: F401


class _OcrEnsemble:
    """Como o ensemble de producao: UMA foto rende varias leituras (3 fast + paddle).

    Existe porque o `_OcrFalso` de `test_payload_leitura` devolve UMA leitura por foto, e
    com ele `n_votos_leitura >= 2` so fecha com duas fotos — o que MASCARA o efeito do piso.
    Com o ensemble real a contagem de votos fecha dentro de uma foto so, e ai quem decide
    quantas fotos o laco gasta e exclusivamente `n_min`.
    """

    def ler_detalhado(self, crop):
        detalhes = [{"engine": nome, "placa": PLACA, "padrao": "mercosul", "confianca": conf}
                    for nome, conf in (("fast_a", 0.98), ("fast_b", 0.99),
                                       ("fast_c", 0.99), ("paddleocr", 0.95))]
        return {"placa": PLACA, "padrao": "mercosul", "confianca": 0.98,
                "votos": 4, "total_engines": 4, "detalhes": detalhes}


def _fonte(camera_id: int = 7, papel: str = "traseira") -> FonteLeitura:
    return FonteLeitura(camera_id=camera_id, papel=papel,
                        especificacao=_especificacao(), roi=None,
                        provider=_provedor_de_frames())


def _ler(n_min: str, **cfg_extra):
    """Uma leitura com `snapshots_votacao=n_min`.

    NAO recebe o duble de OCR: quem instala detector e OCR e a fixture `visao_falsa`, e
    cada teste ja a chama antes. Uma versao anterior aceitava `ocr` e `ambiente` e IGNORAVA
    os dois — o chamador passava `_OcrEnsemble()` novo que nunca era instalado, convidando
    um teste futuro a exercitar silenciosamente o duble errado.
    """
    return leitura_mod.ler_placa(fontes=[_fonte()], cfg={**CFG, "snapshots_votacao": n_min,
                                                         **cfg_extra},
                                 preview_nome=f"preview_bico_{BICO_ID}",
                                 bico_id=BICO_ID, origem="teste")


class TestPisoDeFrames:
    """`snapshots_votacao` decide quantas FOTOS o laco gasta com evidencia ja perfeita."""

    @pytest.mark.parametrize("n_min,frames_esperados", [("2", 2), ("3", 3), ("4", 4)])
    def test_o_piso_manda_mesmo_com_evidencia_perfeita_na_primeira_foto(
            self, n_min, frames_esperados, ambiente, visao_falsa):
        """Todas as fotos concordam a 0,98+: a evidencia nao melhora da 2a em diante.

        Mesmo assim o laco gasta exatamente `n_min` fotos, porque `tentativas >= n_min`
        vem ANTES de qualquer avaliacao de consenso. E o custo puro do piso.
        """
        visao_falsa(_DetectorFalso(), _OcrEnsemble())
        r = _ler(n_min)

        assert r["placa"] == PLACA
        assert r["tentativas"] == frames_esperados
        assert r["parada_motivo"] == "acordo", "parou por consenso, nao por timeout/tentativas"
        assert r["confirmada"] is True

    def test_com_ensemble_o_piso_1_para_numa_foto_so(self, ambiente, visao_falsa):
        """Por que 1 nao serve, e por que isso NAO aparece com um duble de 1 engine.

        Com varias leituras por foto, `n_votos_leitura >= 2` fecha dentro da PRIMEIRA — e
        entao `n_min=1` deixa o laco parar sem nenhuma segunda foto para confirmar. As 4
        leituras sao do MESMO recorte: se o detector entregou um falso positivo, elas
        concordam sobre o falso positivo.
        """
        visao_falsa(_DetectorFalso(), _OcrEnsemble())
        r = _ler("1")

        assert r["tentativas"] == 1, "com ensemble, n_min=1 nao exige segunda foto"
        assert r["confirmada"] is True, "e ainda assim sai confirmada — o risco do piso 1"

    def test_dois_e_o_menor_piso_que_exige_duas_fotos(self, ambiente, visao_falsa):
        """`n_min=2` e o menor valor que preserva concordancia ENTRE fotos."""
        visao_falsa(_DetectorFalso(), _OcrEnsemble())
        assert _ler("2")["tentativas"] == 2


class TestLogContrafactual:
    """`leitura_log_parcial` OBSERVA a foto 2 sem mudar quando o laço para.

    Esta e a propriedade que faz o log valer: se ligar a flag mudasse a parada, o log
    mediria a si mesmo em vez de medir a producao — e a resposta para "baixar
    `snapshots_votacao` para 2 muda a placa?" viria contaminada pela propria instrumentacao.
    """

    def test_a_flag_nao_muda_a_parada_nem_o_resultado(self, ambiente, visao_falsa, caplog):
        visao_falsa(_DetectorFalso(), _OcrEnsemble())
        sem = _ler("3")

        visao_falsa(_DetectorFalso(), _OcrEnsemble())
        with caplog.at_level(logging.INFO, logger="app.visao.leitura"):
            com = _ler("3", leitura_log_parcial="sim")

        assert com["tentativas"] == sem["tentativas"] == 3
        assert com["placa"] == sem["placa"] == PLACA
        assert com["parada_motivo"] == sem["parada_motivo"]
        assert com["confirmada"] == sem["confirmada"]

    def test_registra_a_foto_2_que_e_a_pergunta(self, ambiente, visao_falsa, caplog):
        """`pararia` na foto 2 e o campo que decide `snapshots_votacao=2`."""
        visao_falsa(_DetectorFalso(), _OcrEnsemble())
        with caplog.at_level(logging.INFO, logger="app.visao.leitura"):
            _ler("3", leitura_log_parcial="sim")

        linhas = [r.message for r in caplog.records if "leitura-parcial" in r.message]
        assert any("foto=2" in l for l in linhas), "a foto 2 nao foi registrada"
        assert any("pararia=" in l for l in linhas)

    def test_nao_existe_coluna_confirmaria(self, ambiente, visao_falsa, caplog):
        """Regressao: `confirmaria` era `confirmada(acordo, votos, min, 2)`, que expande
        para `acordo >= min and votos >= 2` — exatamente `pararia`. Medido identico em 20
        combinacoes de (acordo, votos). Coluna sempre igual a outra nao informa e engana."""
        visao_falsa(_DetectorFalso(), _OcrEnsemble())
        with caplog.at_level(logging.INFO, logger="app.visao.leitura"):
            _ler("3", leitura_log_parcial="sim")

        linhas = [r.message for r in caplog.records if "leitura-parcial" in r.message]
        assert linhas
        assert not any("confirmaria" in l for l in linhas)

    @pytest.mark.parametrize("acordo", [0.5, 0.79, 0.80, 0.95, 1.0])
    @pytest.mark.parametrize("votos", [1, 2, 3, 8])
    def test_confirmaria_seria_identico_a_pararia(self, acordo, votos):
        """A prova de que a coluna removida era redundante, nao um descuido."""
        pararia = acordo >= 0.80 and votos >= 2
        assert confirmada(acordo, votos, 0.80, 2) is pararia

    def test_desligada_por_padrao_nao_emite_nada(self, ambiente, visao_falsa, caplog):
        """Default "nao": producao nao paga a eleicao extra nem enche o log."""
        visao_falsa(_DetectorFalso(), _OcrEnsemble())
        with caplog.at_level(logging.INFO, logger="app.visao.leitura"):
            _ler("3")
        assert not [r for r in caplog.records if "leitura-parcial" in r.message]


class TestPisoNaoMexeEmConfirmada:
    """Baixar 3 -> 2 nao afrouxa `confirmada`; baixar para 1 afrouxa."""

    @pytest.mark.parametrize("n_min", [2, 3, 4, 12])
    def test_de_dois_para_cima_exige_sempre_dois_votos(self, n_min):
        """`min(2, n_min)` satura: 2, 3, 4 e 12 sao a MESMA regra de confirmacao."""
        assert confirmada(0.9, 1, 0.80, n_min) is False
        assert confirmada(0.9, 2, 0.80, n_min) is True

    def test_piso_um_aceita_voto_unico(self):
        """O unico valor que muda a regra — e o motivo de `rapido_snapshots_votacao=1`
        merecer atencao: em modo rapido uma leitura de voto unico volta confirmada."""
        assert confirmada(0.9, 1, 0.80, 1) is True


class TestAcordoAlternativo:
    """`com_alternativa` calcula a OUTRA escala de acordo, so para instrumentacao.

    Serve para uma campanha unica responder duas perguntas: o piso de fotos (`pararia`) e
    se `acordo_metrica=caractere` resolveria os casos de leitura certa cujo acordo nao
    fecha. A coleta depende do movimento do posto, entao rodar duas campanhas custaria dias.
    """

    @staticmethod
    def _candidatos():
        """Duas fotos que discordam em 1 caractere — o caso em que as escalas divergem.

        Por string: uma das duas leituras bate com a eleita, entao o acordo e ~0,5.
        Por caractere: elas concordam em 6 das 7 posicoes, entao o acordo e ~0,9.
        """
        def c(placa, conf):
            return {"placa": placa, "confianca": conf, "padrao": "mercosul",
                    "detalhes_ocr": [{"engine": "fast", "placa": placa,
                                      "padrao": "mercosul", "confianca": conf}]}
        return [c("ABC1D23", 0.95), c("ABC1D28", 0.95)]

    def test_sem_o_parametro_a_chave_nem_existe(self):
        """Producao nao paga o calculo extra."""
        r = leitura_mod._eleger_placa(self._candidatos(), "string")
        assert "acordo_alt" not in r

    def test_com_o_parametro_traz_a_OUTRA_escala(self):
        por_string = leitura_mod._eleger_placa(self._candidatos(), "string",
                                               com_alternativa=True)
        por_char = leitura_mod._eleger_placa(self._candidatos(), "caractere",
                                            com_alternativa=True)
        # o `acordo` de uma e o `acordo_alt` da outra, e vice-versa
        assert por_string["acordo_alt"] == pytest.approx(por_char["acordo"], abs=1e-3)
        assert por_char["acordo_alt"] == pytest.approx(por_string["acordo"], abs=1e-3)
        # e elas realmente divergem neste caso, senao o teste nao provaria nada
        assert por_string["acordo"] != pytest.approx(por_char["acordo"], abs=1e-2)

    def test_nao_muda_a_placa_nem_o_acordo_principal(self):
        """Instrumentacao nao pode mexer no numero que a producao consome."""
        sem = leitura_mod._eleger_placa(self._candidatos(), "string")
        com = leitura_mod._eleger_placa(self._candidatos(), "string", com_alternativa=True)
        assert com["placa"] == sem["placa"]
        assert com["acordo"] == sem["acordo"]
        assert com["confianca"] == sem["confianca"]

    def test_acordo_alt_nunca_chega_ao_payload(self, ambiente, visao_falsa):
        """Guarda: `acordo` publico tem de ser UM numero, o da metrica configurada.

        A eleicao final de `_ler_placa` nao passa `com_alternativa`, entao a chave nao
        existe la — mas isso e um invariante a duas inferencias de distancia, e o tipo de
        coisa que um refactor futuro quebra em silencio.
        """
        visao_falsa(_DetectorFalso(), _OcrEnsemble())
        r = _ler("3", leitura_log_parcial="sim")
        assert "acordo_alt" not in r, "instrumentacao vazou para a resposta do roteador"

    def test_caractere_INFLA_quando_as_fotos_veem_veiculos_diferentes(self):
        """A armadilha que a campanha tem de saber ler, e a razao de `string` ser o default.

        Medido: duas fotos lendo placas COMPLETAMENTE diferentes (`ABC1D23` e `XYZ9Q88`)
        dao acordo 0,500 por string e **1,000** por caractere. A causa nao e bug: por
        caractere o acordo e medido sobre o GRUPO vencedor de `agrupar_por_veiculo`, e o
        grupo ja descartou a outra leitura — sobra uma leitura sozinha, que concorda 100%
        consigo mesma.

        Ou seja, trocar `acordo_metrica` para `caractere` conserta o caso "1 caractere de
        ruido no mesmo veiculo" E QUEBRA o caso "as duas cameras enxergam veiculos
        diferentes", que e justamente onde `confirmada=False` esta protegendo o roteador.
        Ver `fusao-precisa-de-duas-trancas`: "o denominador e o pool INTEIRO, e isto e
        deliberado... trocar para o grupo INFLA o acordo".

        Na campanha, `votos_snap` desempata: `acordo_alt` alto com `votos_snap=1` e o caso
        inflado, nao o caso bom.
        """
        def c(placa):
            return {"placa": placa, "confianca": 0.95, "padrao": "mercosul",
                    "detalhes_ocr": [{"engine": "fast", "placa": placa,
                                      "padrao": "mercosul", "confianca": 0.95}]}

        ruido_1_char = leitura_mod._eleger_placa(
            [c("ABC1D23"), c("ABC1D28")], "string", com_alternativa=True)
        veiculos_distintos = leitura_mod._eleger_placa(
            [c("ABC1D23"), c("XYZ9Q88")], "string", com_alternativa=True)

        # nas duas a metrica string da o mesmo: 1 de 2 leituras bate com a eleita
        assert ruido_1_char["acordo"] == pytest.approx(0.5, abs=0.01)
        assert veiculos_distintos["acordo"] == pytest.approx(0.5, abs=0.01)

        # mas por caractere o caso PERIGOSO pontua MAIS que o caso benigno
        assert veiculos_distintos["acordo_alt"] > ruido_1_char["acordo_alt"]
        assert veiculos_distintos["acordo_alt"] == pytest.approx(1.0, abs=0.01)
        # e o sinal que denuncia: uma unica foto sustentando a eleita
        assert veiculos_distintos["n_votos_snap"] == 1


class TestTrancaDeFotos:
    """`n_fotos` impede confirmar apoiado em leituras que vieram todas da MESMA foto.

    Motivada por medicao de campo (01/09/2026): das 4 chamadas em que a parada fecharia na
    2a foto, uma tinha `votos_snap=1, cands=1` — 4 leituras de engine sobre UM recorte.
    """

    def test_duas_leituras_de_uma_foto_so_nao_confirmam(self):
        """O caso `QFB3107`: acordo alto, 4 leituras de engine, mas UMA foto."""
        assert confirmada(0.90, 4, 0.80, 3) is True, "sem a tranca, passava"
        assert confirmada(0.90, 4, 0.80, 3, n_fotos=1) is False
        assert confirmada(0.90, 4, 0.80, 3, n_fotos=2) is True

    def test_nao_substitui_a_regra_das_leituras(self):
        """A regra dos 2 votos foi calibrada (86% reais / 4% falsos). As duas SOMAM."""
        assert confirmada(0.90, 1, 0.80, 3, n_fotos=5) is False, "1 leitura nao passa"
        assert confirmada(0.50, 9, 0.80, 3, n_fotos=5) is False, "acordo baixo nao passa"

    @pytest.mark.parametrize("n_min", [2, 3, 4, 12])
    def test_satura_igual_a_regra_de_votos(self, n_min):
        """`min(2, n_min)` nas duas trancas: 2 fotos bastam, nao `n_min` fotos."""
        assert confirmada(0.90, 4, 0.80, n_min, n_fotos=2) is True
        assert confirmada(0.90, 4, 0.80, n_min, n_fotos=1) is False

    def test_perfil_rapido_nao_e_quebrado_por_esta_mudanca(self):
        """Com `n_min=1` a tranca exige 1 foto — o rapido segue funcionando como antes.

        Que ele CONFIRME com uma foto so continua sendo um problema (medido: 4 de 9
        leituras divergindo, todas confirmadas), mas o conserto e subir
        `rapido_snapshots_votacao`, nao esta funcao.
        """
        assert confirmada(1.0, 3, 0.80, 1, n_fotos=1) is True

    def test_continuo_nao_passa_n_fotos_e_nao_muda(self):
        """`pipeline.py` chama sem `n_fotos`: la um voto JA E um frame distinto."""
        assert confirmada(0.90, 2, 0.80, 3) is True
        assert confirmada(0.90, 1, 0.80, 3) is False
