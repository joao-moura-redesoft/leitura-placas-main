"""A confiança POR CARACTERE tem de chegar viva até a fusão — achado K6.

`_leituras_do_engine` fazia `[(t, c) for t, c in engine.ler_varias(crop) if t]`. A
comprehension remonta tuplas nuas e joga fora o `por_char` das `LeituraOCR`, então
`getattr(leitura, "por_char", None)` em `_ler_com_engines` devolvia None SEMPRE e cada
leitura votava com o escalar (a média) em todas as sete posições.

O mecanismo central do ensemble esteve morto desde o commit que o criou (8f5dc4f, 26/08/2026)
e nenhum teste cobria o trajeto — daí este arquivo. Ele trava o CAMINHO (o vetor sobrevive ao
helper) e o EFEITO (a fusão usa o vetor, e o resultado muda por causa dele).

Cuidado ao mexer: as asserções aqui são valores escritos à mão. Recalcular o esperado com a
mesma expressão da implementação faria o teste passar com a feature desligada de novo — foi
assim que 27 testes ficaram verdes com o bug em pé neste projeto.
"""
from __future__ import annotations

import pytest

from app.visao.ocr.auto import _leituras_do_engine
from app.visao.ocr.engines import LeituraOCR


class _EngineComVetor:
    """Devolve `LeituraOCR` com `por_char`, como o fast_plate_ocr real."""

    def __init__(self, leituras):
        self._leituras = leituras

    def ler_varias(self, crop):
        return list(self._leituras)


class _EngineAntigo:
    """Dublê que só implementa `ler()` — o contrato antigo tem de continuar funcionando."""

    def ler(self, crop):
        return ("ABC1D23", 0.9)


class TestOVetorSobreviveAoHelper:
    def test_por_char_chega_intacto(self):
        vetor = [0.99, 0.30, 0.97, 0.99, 0.22, 0.99, 0.99]
        eng = _EngineComVetor([LeituraOCR("BLX2677", 0.78, vetor)])

        (leitura,) = _leituras_do_engine(eng, None)

        assert getattr(leitura, "por_char", None) == vetor, (
            "o helper remontou a tupla e descartou o vetor — é exatamente o bug K6"
        )

    def test_continua_desempacotavel_como_par(self):
        """O contrato de 2-tupla é o que permite `for t, c in ...` em todo consumidor."""
        eng = _EngineComVetor([LeituraOCR("ABC1D23", 0.9, [0.9] * 7)])
        (leitura,) = _leituras_do_engine(eng, None)
        texto, conf = leitura
        assert (texto, conf) == ("ABC1D23", 0.9)
        assert leitura[0] == "ABC1D23" and leitura[1] == 0.9

    def test_leitura_vazia_continua_sendo_filtrada(self):
        eng = _EngineComVetor([
            LeituraOCR("", 0.5, None),
            LeituraOCR("ABC1D23", 0.9, [0.9] * 7),
        ])
        assert [l[0] for l in _leituras_do_engine(eng, None)] == ["ABC1D23"]

    def test_engine_antigo_sem_ler_varias_nao_quebra(self):
        """Os dublês de teste implementam só `ler()`; degradar é obrigatório."""
        (leitura,) = _leituras_do_engine(_EngineAntigo(), None)
        assert leitura == ("ABC1D23", 0.9)
        assert getattr(leitura, "por_char", None) is None

    def test_engine_que_explode_custa_so_o_voto(self):
        class _Explode:
            def ler_varias(self, crop):
                raise RuntimeError("modelo caiu")

        assert _leituras_do_engine(_Explode(), None) == []


class TestAFusaoRealmenteUsaOVetor:
    """Não basta o vetor chegar: ele tem de MUDAR a decisão. Estes casos comparam a mesma
    votação com e sem `por_char` e exigem resultados diferentes."""

    def test_o_vetor_muda_a_decisao_da_fusao(self):
        """O vetor tem de ALTERAR a conta — senão o `por_char` voltou a ser descartado.

        Não afirma "com vetor acerta mais" num caso inventado, e a razão é concreta: ao
        escrever este teste, a primeira tentativa usava dados sintéticos e "provava" que o
        vetor piorava o resultado. A medição no dataset real disse o contrário
        (42/54 = 77,8% com vetor contra 41/54 = 75,9% sem, em 27/08/2026). Caso sintético
        não decide acurácia neste projeto — ver [[sem-fotos-sinteticas]]. Quem mede é
        `testes/run_testes.py`; o que se trava AQUI é só o mecanismo estar ligado.

        Dados reais do recorte da RLX2A77: `BLX2677` sai com
        `[0.99, 0.30, 0.97, 0.99, 0.22, 0.99, 0.99]`, e contra a verdade as leituras divergem
        em duas posições — a 0 (`B` vs `R`, conf 0.99: o modelo está confiante e errado) e a
        4 (`6` vs `A`, conf 0.22: incerto e errado). O vetor alcança a 4, não a 0. É o limite
        que [[peso-por-caractere-nao-basta]] registra: quando nenhum modelo propõe o caractere
        certo com confiança, nenhuma ponderação salva.
        """
        from app.visao.consenso import consenso_caractere

        errada_txt = "BLX2677"
        errada_vec = [0.99, 0.30, 0.97, 0.99, 0.22, 0.99, 0.99]
        certa_txt = "RLX2A77"
        certa_vec = [0.95] * 7

        com_vetor = consenso_caractere([(errada_txt, errada_vec), (certa_txt, certa_vec)])
        # O escalar equivalente é a média do vetor (0.78) — o que o bug K6 usava.
        sem_vetor = consenso_caractere([(errada_txt, 0.78), (certa_txt, 0.95)])

        assert com_vetor != sem_vetor, (
            "o vetor não mudou a conta — provavelmente `por_char` voltou a ser descartado "
            "em `_leituras_do_engine` (achado K6)"
        )
        # Valores escritos à mão, não recalculados pela implementação: com o vetor a posição
        # 4 resolve para 'A' e a 0 continua 'B'; com o escalar, a leitura confiante leva tudo.
        assert com_vetor == "BLX2A77"
        assert sem_vetor == "RLX2A77"

    def test_duas_leituras_da_mesma_familia_nao_amplificam_o_erro(self):
        """O caso que motivou a fusão por posição: dois modelos `cct-*` erram IGUAL, e o
        escalar deixa os dois somarem peso total contra um terceiro que acertou."""
        from app.visao.consenso import consenso_caractere

        # Os dois primeiros erram na posição 4 e SABEM disso (0.20); o terceiro acerta.
        pool = [
            ("RLX2677", [0.98, 0.98, 0.98, 0.98, 0.20, 0.98, 0.98]),
            ("RLX2677", [0.97, 0.97, 0.97, 0.97, 0.20, 0.97, 0.97]),
            ("RLX2A77", [0.90, 0.90, 0.90, 0.90, 0.90, 0.90, 0.90]),
        ]
        assert consenso_caractere(pool) == "RLX2A77"


class TestArmadilhaDoNdarray:
    """`por_char or conf` (auto.py) só é seguro porque o alinhamento devolve `list`. Um
    ndarray faria o `or` estourar `ValueError: truth value of an array is ambiguous` no meio
    da passada de OCR — o mesmo crash de numpy que este projeto já teve em 20/07."""

    def test_alinhar_por_char_devolve_list_e_nao_ndarray(self):
        np = pytest.importorskip("numpy")
        from app.visao.ocr.engines import _alinhar_por_char

        saida = _alinhar_por_char("ABC1D23", np.array([0.9] * 10, dtype=np.float32), "teste")
        assert saida is None or isinstance(saida, list), (
            "tem de ser list: `por_char or conf` com ndarray levanta ValueError"
        )
        if saida is not None:
            assert all(isinstance(v, float) for v in saida)
            # `or` sobre a lista tem de ser avaliável sem estourar.
            assert (saida or 0.5) is saida
