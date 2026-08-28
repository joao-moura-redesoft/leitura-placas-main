"""Consenso do monitoramento CONTÍNUO — o número que decide o selo "a conferir".

Até aqui o pipeline gravava `acordo`/`confirmada` como NULL, e o histórico só marca
"a conferir" quando `confirmada` é 0. O efeito: uma leitura fraca do contínuo ficava
visualmente IDÊNTICA a uma sólida na mesma tabela, e a única origem que declarava a
própria qualidade era a leitura reativa.

Estes testes cobrem os dois modos de emissão do contínuo (tracker e clássico) e a
tradução deles para a regra compartilhada de `app/visao/consenso.py`.
"""
from __future__ import annotations

from collections import deque

import numpy as np
import pytest

from app.visao.consenso import confirmada
from app.visao.pipeline import Pipeline, _consenso_janela, _maxlen_historico
from app.visao.tracker import Tracker

FRAME = np.zeros((480, 640, 3), dtype=np.uint8)
BBOX = [(100, 100, 120, 40, 0.9)]


# ── Modo clássico: janela de frames ───────────────────────────────────────────

class TestConsensoJanela:
    def test_janela_toda_igual_e_acordo_total(self):
        assert _consenso_janela(["ABC1D23"] * 3, "ABC1D23") == (1.0, 3)

    def test_janela_vazia_nao_divide_por_zero(self):
        """Não deve acontecer (só se chama após uma emissão), mas 0/0 derrubaria a
        thread do pipeline inteiro — e uma câmera que morre em silêncio é pior do que
        uma detecção sem consenso."""
        assert _consenso_janela([], "ABC1D23") == (0.0, 0)

    def test_leituras_do_veiculo_anterior_derrubam_o_acordo(self):
        """Sem tracker não há noção de veículo: o que sobrou de um carro que nunca
        fechou consenso continua na janela. Subestimar aqui é a escolha deliberada —
        o erro cai do lado de pedir conferência a mais, não a menos."""
        acordo, votos = _consenso_janela(["XYZ9K88", "XYZ9K88"] + ["ABC1D23"] * 3, "ABC1D23")
        assert votos == 3
        assert acordo == pytest.approx(0.6)


def _pipeline_classico(frames_consenso: int = 3, acordo_min: float = 0.80):
    """Pipeline sem `__init__` — mesmo padrão de test_pipeline_loop.py: `__init__` criaria
    Camera/detector/OCR de verdade. Só o que `_tentar_emitir` toca é preenchido, e
    `_emitir` é substituído para capturar o que teria ido ao banco."""
    p = Pipeline.__new__(Pipeline)
    p.frames_consenso = frames_consenso
    p.acordo_min = acordo_min
    # Mesma conta do `__init__` real (`_maxlen_historico`): o deque precisa comportar
    # PELO MENOS frames_consenso itens, senão `recentes` nunca alcança esse tamanho e
    # a emissão trava em silêncio — é o bug que TestFramesConsensoAcimaDoPadrao cobre.
    p._historico = deque(maxlen=_maxlen_historico(frames_consenso))
    p.emitidas = []
    p._emitir = lambda *args, **kw: p.emitidas.append(kw)
    return p


class TestTentarEmitirClassico:
    def test_nao_emite_antes_de_fechar_consenso(self):
        p = _pipeline_classico(frames_consenso=3)
        for _ in range(2):
            p._tentar_emitir("ABC1D23", "mercosul", 0.9, FRAME, (0, 0, 10, 10))
        assert p.emitidas == []

    def test_tres_frames_iguais_confirmam(self):
        p = _pipeline_classico(frames_consenso=3)
        for _ in range(3):
            p._tentar_emitir("ABC1D23", "mercosul", 0.9, FRAME, (0, 0, 10, 10))
        assert len(p.emitidas) == 1
        assert p.emitidas[0]["acordo"] == 1.0
        assert p.emitidas[0]["confirmada"] is True
        # `votos`/`total_leituras` não decidem nada — vão só para a linha de log da
        # emissão, onde "3/3" é o que separa consenso real de acordo por falta de dado.
        assert (p.emitidas[0]["votos"], p.emitidas[0]["total_leituras"]) == (3, 3)

    def test_janela_suja_emite_mas_marca_para_conferencia(self):
        """A emissão continua acontecendo — o consenso exigido para EMITIR não mudou.
        O que muda é que a linha nasce marcada, em vez de nascer indistinguível."""
        p = _pipeline_classico(frames_consenso=3)
        for placa in ["XYZ9K88", "XYZ9K88", "ABC1D23", "ABC1D23", "ABC1D23"]:
            p._tentar_emitir(placa, "mercosul", 0.9, FRAME, (0, 0, 10, 10))
        assert len(p.emitidas) == 1
        assert p.emitidas[0]["acordo"] == pytest.approx(0.6)
        assert p.emitidas[0]["confirmada"] is False

    def test_frames_consenso_1_respeita_quem_desligou_a_votacao(self):
        """Mesma semântica de `snapshots_votacao=1` na leitura reativa: exigir 2 votos
        de quem configurou 1 deixaria TODA detecção não-confirmada."""
        p = _pipeline_classico(frames_consenso=1)
        p._tentar_emitir("ABC1D23", "mercosul", 0.9, FRAME, (0, 0, 10, 10))
        assert len(p.emitidas) == 1
        assert p.emitidas[0]["acordo"] == 1.0
        assert p.emitidas[0]["confirmada"] is True

    def test_historico_zerado_na_emissao_nao_reemite_de_graca(self):
        """Regressão do `clear()`: o acordo é medido ANTES dele. Se a ordem inverter,
        a janela chega vazia em `_consenso_janela` e todo acordo vira 0.0."""
        p = _pipeline_classico(frames_consenso=3)
        for _ in range(4):
            p._tentar_emitir("ABC1D23", "mercosul", 0.9, FRAME, (0, 0, 10, 10))
        assert len(p.emitidas) == 1
        assert p.emitidas[0]["acordo"] == 1.0


class TestFramesConsensoAcimaDoPadrao:
    """Regressão: `self._historico` tinha `maxlen=10` fixo em `__init__`, mas a UI de
    config permite `frames_consenso` de 1 a 20 sem validação server-side. Com
    `frames_consenso > 10`, `recentes = list(self._historico)[-self.frames_consenso:]`
    nunca alcançava `len == self.frames_consenso` (o deque descartava o excedente antes
    disso) — a emissão parava de acontecer, EM SILÊNCIO, embora o bbox verde continuasse
    sendo desenhado na tela como se estivesse indo pro ar."""

    def test_frames_consenso_15_emite(self):
        p = _pipeline_classico(frames_consenso=15)
        for _ in range(15):
            p._tentar_emitir("ABC1D23", "mercosul", 0.9, FRAME, (0, 0, 10, 10))
        assert len(p.emitidas) == 1
        assert p.emitidas[0]["acordo"] == 1.0
        assert p.emitidas[0]["confirmada"] is True
        assert (p.emitidas[0]["votos"], p.emitidas[0]["total_leituras"]) == (15, 15)

    def test_frames_consenso_15_nao_emite_antes_de_fechar(self):
        p = _pipeline_classico(frames_consenso=15)
        for _ in range(14):
            p._tentar_emitir("ABC1D23", "mercosul", 0.9, FRAME, (0, 0, 10, 10))
        assert p.emitidas == []

    @pytest.mark.parametrize("frames_consenso,esperado", [
        (1, 10), (5, 10), (10, 10),   # piso: comportamento inalterado até 10
        (11, 11), (15, 15), (20, 20),  # acima de 10: o deque precisa acompanhar
    ])
    def test_maxlen_historico_acompanha_frames_consenso_acima_do_piso(self, frames_consenso, esperado):
        """`_maxlen_historico` é a MESMA conta que `Pipeline.__init__` usa para criar
        `self._historico` — testar a função exercita a lógica real de produção, não
        uma cópia dela dentro do teste."""
        assert _maxlen_historico(frames_consenso) == esperado


# ── Modo tracker: votos por veículo rastreado ─────────────────────────────────

@pytest.fixture
def tracker():
    t = Tracker(ocr_a_cada_n_frames=1, votos_emitir=2, paciencia_frames=5)
    t.carregar()
    return t


class TestConsensoTracker:
    def test_track_desconhecido_nao_finge_consenso(self, tracker):
        """(0, 0) e não (1, 1): um veículo que nunca foi lido tem consenso NENHUM."""
        assert tracker.consenso(999) == (0, 0)

    def test_veiculo_visto_e_nao_lido_tem_consenso_zero(self, tracker):
        tid = tracker.update(BBOX, FRAME)[0][5]
        assert tracker.consenso(tid) == (0, 0)

    def test_conta_votos_da_placa_vencedora_sobre_o_total(self, tracker):
        tid = tracker.update(BBOX, FRAME)[0][5]
        for placa in ["ABC1D23", "ABC1D23", "ABC1O23"]:
            tracker.registrar_ocr(tid, placa, "mercosul", 0.9)
        assert tracker.consenso(tid) == (2, 3)

    def test_consenso_sobrevive_a_marcar_emitido(self, tracker):
        """`marcar_emitido` não apaga as leituras — se apagasse, o pipeline gravaria
        acordo 0 em toda detecção do modo tracker."""
        tid = tracker.update(BBOX, FRAME)[0][5]
        tracker.registrar_ocr(tid, "ABC1D23", "mercosul", 0.9)
        tracker.registrar_ocr(tid, "ABC1D23", "mercosul", 0.9)
        tracker.marcar_emitido(tid)
        assert tracker.consenso(tid) == (2, 2)

    def test_votos_minimos_exposto_para_o_pipeline(self, tracker):
        assert tracker.votos_minimos == 2


class TestVereditoNoModoTracker:
    """A tradução (votos, total) → confirmada, com os limiares reais do contínuo."""

    def test_dois_votos_limpos_confirmam(self):
        assert confirmada(2 / 2, 2, 0.80, 2) is True

    def test_um_voto_isolado_nao_confirma(self):
        """`tracker_votos_emitir=1` é config; `votos_emitir` default é 2. Com o default,
        um único OCR não pode emitir — mas se emitisse, não valeria como confirmado."""
        assert confirmada(1.0, 1, 0.80, 2) is False

    def test_veiculo_com_leituras_divergentes_nao_confirma(self):
        """2 de 5 leituras concordando = 0.40: o OCR estava instável neste veículo,
        e essa é exatamente a linha que alguém precisa olhar antes de cobrar."""
        assert confirmada(2 / 5, 2, 0.80, 2) is False
