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
