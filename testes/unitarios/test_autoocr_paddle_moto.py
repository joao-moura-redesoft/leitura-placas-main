"""Como o AutoOCRPaddle combina os engines — e por que não existe mais "quem ganha de quem".

Até 25/08/2026 esta classe ARBITRAVA: escolhia um engine principal pelo layout do recorte,
usava o outro só como fallback, e tinha uma regra extra ("em moto o Paddle sobrepõe") mais
um gate de nitidez ("crop nítido não chama o Paddle se o AutoOCR já validou"). Os testes
antigos deste arquivo fixavam exatamente essa arbitragem.

Duas coisas derrubaram as regras que eles protegiam:

1. A justificativa da prioridade do Paddle em moto era "22/27 contra 2/27 nas 27 motos de
   testes/dataset.json". O dataset tinha 42 fotos e o commit d49a78f
   ("Remove as placas sinteticas do dataset de testes") o cortou para 29 — as 27 motos eram
   SINTÉTICAS, deletadas justamente por inverter o sinal da medição. Hoje há 2 motos reais.
2. A arbitragem em si era onde a leitura certa morria. Em 24/08/2026, no bico 3 do
   ALTIPLANO, o sistema leu `RLX2A77` com confiança 0,96 e todos os char_probs ≥ 0,93, e
   emitiu `HDX2477` — uma leitura de outra passada com dois caracteres abaixo de 0,62.

Agora todos os membros leem sempre e a fusão por caractere decide (`AutoOCR._fundir`). O que
este arquivo fixa passou a ser o que de fato importa: que o pool receba o voto de todo
mundo, que a placa certa saia dele, e que um engine quebrado custe o voto dele e nada mais.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.visao.ocr.auto import AutoOCR, AutoOCRPaddle

CROP = np.zeros((140, 200, 3), dtype=np.uint8)


class EngineFalso:
    """Engine com o contrato ANTIGO (só `ler`) — nenhum `ler_varias`.

    De propósito: `_leituras_do_engine` tem de continuar aceitando quem só sabe devolver
    uma leitura, senão todo dublê existente quebra em silêncio no dia em que o contrato
    cresce (o mesmo modo de falha que `None vs bool em contrato novo` já custou aqui).
    """

    def __init__(self, saida, engine="falso"):
        self.saida = saida
        self.engine = engine
        self.chamadas = 0

    def carregar(self):
        pass

    def ler(self, _crop):
        self.chamadas += 1
        return self.saida


def _montar(fast, paddle=None, easy=None) -> AutoOCRPaddle:
    """AutoOCRPaddle sem `__init__` — não constrói OCR de verdade.

    Todo atributo que `ler_detalhado` lê é setado aqui à mão. Se um atributo novo aparecer
    no código e não aqui, o teste estoura com `AttributeError` em vez de passar medindo
    outra coisa — que é o comportamento desejado para um dublê.
    """
    m = AutoOCRPaddle.__new__(AutoOCRPaddle)
    m.engine = "auto"
    m._ultimo_detalhe = {}
    m._ultimo_e_moto = False
    m._ultimo_formato_hint = ""
    m._fast = fast
    m._easy = easy
    m._paddle = paddle
    return m


@pytest.fixture(autouse=True)
def _sem_header(monkeypatch):
    """Neutraliza a detecção de faixa: o crop dos dublês é preto e não tem faixa nenhuma.

    Sem isto cada teste dependeria do resultado de `_remover_header` num quadrado zerado —
    um detalhe de pré-processamento que não é o que estes testes medem.
    """
    monkeypatch.setattr(
        "app.visao.ocr.engines.OCR._remover_header",
        lambda self, crop: (crop, False, False), raising=True,
    )


class TestPoolPlano:
    def test_todos_os_membros_votam(self):
        """O Paddle não é mais "reforço condicional": ele lê sempre, como os outros."""
        fast = EngineFalso(("ABC1D23", 0.90), "fast_plate_ocr")
        paddle = EngineFalso(("ABC1D23", 0.80), "paddleocr")
        d = _montar(fast, paddle).ler_detalhado(CROP)

        assert fast.chamadas == 1 and paddle.chamadas == 1
        assert [x["engine"] for x in d["detalhes"]] == ["fast_plate_ocr", "paddleocr"]
        assert d["placa"] == "ABC1D23"
        assert d["total_engines"] == 2

    def test_easyocr_fica_fora_do_pool_quando_nao_pedida(self):
        """`usar_easyocr=nao` é o default — medido, não suposto (ver config.py)."""
        fast = EngineFalso(("ABC1D23", 0.90), "fast_plate_ocr")
        d = _montar(fast, paddle=EngineFalso(("ABC1D23", 0.8), "paddleocr")).ler_detalhado(CROP)
        assert "easyocr" not in [x["engine"] for x in d["detalhes"]]

    def test_maioria_por_posicao_vence_a_leitura_mais_confiante(self):
        """O caso RLX2A77: a placa certa sai do VOTO, não de quem tem mais confiança.

        `XYZ9K88` chega com 0,99 e é a leitura mais confiante do lote; as outras três
        concordam entre si posição a posição. A arbitragem antiga entregaria `XYZ9K88`.
        """
        fast = EngineFalso(("ABC1D23", 0.70), "fast_plate_ocr")
        easy = EngineFalso(("ABC1D23", 0.72), "easyocr")
        paddle = EngineFalso(("XYZ9K88", 0.99), "paddleocr")
        d = _montar(fast, paddle, easy).ler_detalhado(CROP)
        assert d["placa"] == "ABC1D23"

    def test_um_caractere_ruim_em_cada_leitura_ainda_converge(self):
        """Nenhuma das três leituras está certa; a placa certa só existe no voto.

        É o mecanismo inteiro num teste: `RLX2A77` não é nenhuma das entradas.
        """
        fast = EngineFalso(("RLT2A77", 0.90), "fast_plate_ocr")
        easy = EngineFalso(("RLX2A77", 0.88), "easyocr")
        paddle = EngineFalso(("RLX2477", 0.86), "paddleocr")
        d = _montar(fast, paddle, easy).ler_detalhado(CROP)
        assert d["placa"] == "RLX2A77"


class TestNaoInventaPlaca:
    def test_leituras_de_veiculos_diferentes_nao_geram_uma_terceira(self):
        """Duas leituras distantes são veículos distintos — vence o grupo, não a mistura."""
        fast = EngineFalso(("OSL2G55", 0.85), "fast_plate_ocr")
        paddle = EngineFalso(("FWX9760", 0.74), "paddleocr")
        d = _montar(fast, paddle).ler_detalhado(CROP)
        assert d["placa"] in ("OSL2G55", "FWX9760")

    def test_placa_emitida_sempre_saiu_de_alguma_leitura(self):
        lidas = ["OSL2G55", "FWX9760", "ABC1D23"]
        d = _montar(EngineFalso((lidas[0], 0.85), "fast_plate_ocr"),
                    EngineFalso((lidas[1], 0.80), "paddleocr"),
                    EngineFalso((lidas[2], 0.80), "easyocr")).ler_detalhado(CROP)
        assert any(sum(1 for a, b in zip(d["placa"], p) if a != b) <= 2 for p in lidas)


class TestSemHintDeMotoMercosul:
    """O hint `mercosul_moto` reescrevia caractere sem ver a confiança por caractere.

    Na moto antiga metálica OSL2659 o detector de faixa deu falso positivo, o hint entrou, e
    `validar('OSL2655', 'mercosul_moto')` trocou a posição 4 — que o modelo havia lido com
    0,99 — devolvendo `OSL2G55`: um erro de 1 caractere virou 2 e o padrão inverteu.
    """

    def test_validador_nao_conhece_mais_o_hint_forte(self):
        from app.visao.validador import validar
        assert validar("OSL2655", "mercosul_moto") == ("OSL2655", "antigo")

    def test_hint_desconhecido_nao_quebra_chamador_antigo(self):
        """Passar 'mercosul_moto' virou no-op, não erro.

        Algum chamador fora de `auto.py` — script de replay, harness de acurácia — pode
        ainda passar a string antiga. Ela tem de ser ignorada em silêncio, e não virar
        `KeyError` no meio de uma medição.
        """
        from app.visao.validador import validar
        assert validar("ABC1D23", "mercosul_moto") == validar("ABC1D23", "")

    def test_o_hint_que_sobrou_e_inerte(self):
        """Medido: 0 diferenças em 200.000 strings entre hint '' e 'mercosul'.

        Fixado como teste para que ninguém acredite que passar 'mercosul' corrige algo. Se
        um dia isto falhar, o hint voltou a ter efeito e aí é decisão consciente — não
        descoberta acidental.
        """
        import random
        import string
        from app.visao.validador import validar
        random.seed(7)
        al = string.ascii_uppercase + string.digits
        for _ in range(2000):
            t = "".join(random.choice(al) for _ in range(7))
            assert validar(t, "") == validar(t, "mercosul"), t


class TestFalhaDeUmEngineNaoDerrubaALeitura:
    """Uma exceção de pré-processamento (`cv2.error` de `imdecode` devolvendo None, de
    `getPerspectiveTransform` num quadrilátero degenerado) derrubava a passada inteira: o
    roteador levava 500 em vez do payload degradado, a chamada não virava linha em
    `chamadas`, e a causa real não aparecia no log do app.

    Com pool plano a propriedade fica mais forte do que era: a falha custa o VOTO daquele
    engine, e os outros seguem votando.
    """

    class _Explode:
        engine = "explode"

        def ler(self, _crop):
            raise RuntimeError("cv2 explodiu no pre-processamento")

    def test_engine_que_explode_custa_o_voto_e_nada_mais(self, caplog):
        m = _montar(self._Explode(), EngineFalso(("ABC1D23", 0.88), "paddleocr"))
        with caplog.at_level("ERROR"):
            d = m.ler_detalhado(CROP)

        assert d["placa"] == "ABC1D23"
        assert "cv2 explodiu no pre-processamento" in caplog.text

    def test_faixa_que_explode_nao_impede_o_ocr(self, monkeypatch, caplog):
        """`_remover_header` roda antes de qualquer engine — se ela cair, ninguém lê."""
        monkeypatch.setattr(
            "app.visao.ocr.engines.OCR._remover_header",
            lambda self, crop: (_ for _ in ()).throw(RuntimeError("faixa explodiu")),
            raising=True,
        )
        fast = OcrRealMinimo(("ABC1D23", 0.9))
        with caplog.at_level("ERROR"):
            d = _montar(fast).ler_detalhado(CROP)
        assert d["placa"] == "ABC1D23"
        assert "faixa explodiu" in caplog.text

    def test_todos_explodindo_devolve_sem_leitura_em_vez_de_levantar(self, caplog):
        m = _montar(self._Explode(), self._Explode())
        with caplog.at_level("ERROR"):
            d = m.ler_detalhado(CROP)
        assert d["placa"] is None
        assert d["confianca"] == 0.0


class OcrRealMinimo:
    """Dublê que herda `_remover_header` do OCR de verdade — usado só onde o teste precisa
    que a detecção de faixa REAL seja chamada (para então falhar)."""

    engine = "fast_plate_ocr"

    def __init__(self, saida):
        self.saida = saida

    def ler(self, _crop):
        return self.saida

    def _remover_header(self, crop):
        from app.visao.ocr.engines import OCR
        return OCR._remover_header(self, crop)
