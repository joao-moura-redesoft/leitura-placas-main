"""Equivalência BIT-A-BIT do AjustadorAmbiente otimizado contra a implementação anterior.

`AjustadorAmbiente.processar()` custava ~55ms/frame a 1280x720 (medido nesta máquina) e
rodava a `camera_fps` (15/s) — mais caro que a própria detecção YOLO+OCR (5/s) que o
resultado alimenta. Três etapas foram reescritas para saída EXATAMENTE igual (~29ms):

  1. white-balance + gamma fundidos numa única LUT de 3 canais
  2. ganhos de white-balance via `mean(axis=(0,1), dtype=float32)` em vez de
     `astype(float32).reshape(-1,3).mean(axis=0)` — mesma acumulação, sem copiar
     o frame inteiro
  3. CLAHE sem split/merge, saturação por LUT no canal S

Importante: alternativas que pareciam equivalentes e eram MAIS rápidas (`cv2.mean`,
que acumula em double; `cv2.reduce`, que muda a ordem da soma) foram descartadas
porque o CLAHE amplifica qualquer divergência de precisão de forma imprevisível —
chegou a 43/255 numa cena sintética durante o desenvolvimento. Por isso os testes
abaixo exigem `np.array_equal`, não uma tolerância — qualquer regressão de exatidão
tem que quebrar a suíte, não passar "próximo o suficiente".

`_Referencia` abaixo é uma cópia LITERAL da implementação anterior (git-blame: ver
commit que introduziu este arquivo). Ela existe só para isto — não "arrume" nem
simplifique, o valor dela é continuar sendo o código que rodava em produção antes da
otimização. Se `app.visao.ambiente` mudar de novo, estes testes têm que continuar
comparando contra ESTE congelamento, não contra a versão nova.
"""
from __future__ import annotations

import numpy as np
import pytest
import cv2

from app.core import estado
from app.visao.ambiente import AjustadorAmbiente


class _Referencia:
    """Implementação anterior à otimização — ver docstring do módulo."""

    def __init__(self, cfg, camera_db_id=0):
        from app.visao.ambiente import _bool, _float, _int
        self.camera_db_id = camera_db_id
        self.ativo = _bool(cfg, "ajuste_ambiente", "nao")
        self.brilho_alvo = min(220.0, max(60.0, _float(cfg, "ajuste_brilho_alvo", 120.0)))
        self.forca = min(1.0, max(0.0, _float(cfg, "ajuste_forca", 0.8)))
        self.usar_clahe = _bool(cfg, "ajuste_clahe", "sim")
        self.usar_wb = _bool(cfg, "ajuste_wb", "sim")
        self.usar_saturacao = _bool(cfg, "ajuste_saturacao", "sim")
        self.usar_denoise = _bool(cfg, "ajuste_denoise_noite", "sim")
        self.recalc_n = max(1, _int(cfg, "ajuste_recalc_frames", 8))
        self._frames = 0
        self._perfil = "normal"
        self._expo = 1.0
        self._lut = None
        self._clahe = None
        self._clahe_clip = 0.0
        self._sat_factor = 1.0
        self._denoise = False

    def _stats(self, frame):
        h, w = frame.shape[:2]
        if w > 320:
            fator = 320.0 / w
            pequeno = cv2.resize(frame, (0, 0), fx=fator, fy=fator, interpolation=cv2.INTER_AREA)
        else:
            pequeno = frame
        cinza = cv2.cvtColor(pequeno, cv2.COLOR_BGR2GRAY)
        total = cinza.size
        return {
            "luminancia": float(cinza.mean()),
            "contraste": float(cinza.std()),
            "pct_estourado": float(np.count_nonzero(cinza > 245)) / total,
            "pct_escuro": float(np.count_nonzero(cinza < 15)) / total,
            "saturacao": float(cv2.cvtColor(pequeno, cv2.COLOR_BGR2HSV)[..., 1].mean()),
        }

    @staticmethod
    def _classificar(s):
        lum = s["luminancia"]
        if lum < 45:
            return "noite"
        if lum < 90:
            return "baixa_luz"
        if lum > 175 and s["pct_estourado"] > 0.06:
            return "sol_forte"
        if s["contraste"] < 40:
            return "nublado"
        return "normal"

    def _recalcular(self, frame):
        s = self._stats(frame)
        perfil = self._classificar(s)
        self._perfil = perfil
        mean_norm = min(0.98, max(0.02, s["luminancia"] / 255.0))
        alvo_norm = min(0.98, max(0.05, self.brilho_alvo / 255.0))
        try:
            expo_alvo = np.log(alvo_norm) / np.log(mean_norm)
        except (ValueError, ZeroDivisionError):
            expo_alvo = 1.0
        expo_alvo = float(min(2.5, max(0.4, expo_alvo)))
        self._expo = 0.7 * self._expo + 0.3 * expo_alvo
        self._lut = self._montar_lut(self._expo)
        clip = {"nublado": 4.0, "noite": 3.0, "baixa_luz": 2.5,
                "sol_forte": 1.5, "normal": 2.0}.get(perfil, 2.0)
        if self._clahe is None or abs(clip - self._clahe_clip) > 0.1:
            self._clahe = cv2.createCLAHE(clipLimit=clip, tileGridSize=(8, 8))
            self._clahe_clip = clip
        self._sat_factor = {"noite": 1.15, "baixa_luz": 1.10, "nublado": 1.15,
                            "sol_forte": 0.90, "normal": 1.0}.get(perfil, 1.0)
        self._denoise = perfil in ("noite", "baixa_luz")
        estado.registrar_ambiente(self.camera_db_id, perfil,
                                  luminancia=round(s["luminancia"], 1),
                                  contraste=round(s["contraste"], 1),
                                  gamma=round(self._expo, 2))

    @staticmethod
    def _montar_lut(expo):
        x = np.arange(256, dtype=np.float32) / 255.0
        return np.clip((x ** expo) * 255.0, 0, 255).astype(np.uint8)

    @staticmethod
    def _white_balance(img):
        f = img.astype(np.float32)
        medias = f.reshape(-1, 3).mean(axis=0)
        cinza = float(medias.mean())
        for c in range(3):
            m = float(medias[c])
            if m > 1e-3:
                f[..., c] *= cinza / m
        return np.clip(f, 0, 255).astype(np.uint8)

    def _clahe_l(self, img):
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        l = self._clahe.apply(l)
        return cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)

    @staticmethod
    def _saturar(img, fator):
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).astype(np.float32)
        hsv[..., 1] = np.clip(hsv[..., 1] * fator, 0, 255)
        return cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)

    def processar(self, frame):
        if not self.ativo or frame is None or frame.size == 0:
            return frame
        try:
            self._frames += 1
            if self._lut is None or self._frames % self.recalc_n == 1:
                self._recalcular(frame)
            trabalho = frame
            if self.usar_wb:
                trabalho = self._white_balance(trabalho)
            if self._lut is not None:
                trabalho = cv2.LUT(trabalho, self._lut)
            if self.usar_clahe and self._clahe is not None:
                trabalho = self._clahe_l(trabalho)
            if self.usar_saturacao and abs(self._sat_factor - 1.0) > 0.01:
                trabalho = self._saturar(trabalho, self._sat_factor)
            if self.usar_denoise and self._denoise:
                trabalho = cv2.bilateralFilter(trabalho, 5, 45, 45)
            if self.forca >= 0.999:
                return trabalho
            if self.forca <= 0.001:
                return frame
            return cv2.addWeighted(frame, 1.0 - self.forca, trabalho, self.forca, 0.0)
        except Exception:
            return frame


# ── Frames sintéticos ────────────────────────────────────────────────────────
# Tamanho real (1280x720): é com muitos pixels acumulados que uma composição errada
# de LUT ou uma mudança de precisão na soma se manifestaria — num frame pequeno o
# efeito seria pequeno demais pro teste provar alguma coisa.

def _frame(cor=(120, 130, 110), ruido=25, tamanho=(720, 1280)):
    rng = np.random.default_rng(42)
    h, w = tamanho
    base = np.array(cor, dtype=np.float32)
    img = base + rng.normal(0, ruido, (h, w, 3)).astype(np.float32)
    return np.clip(img, 0, 255).astype(np.uint8)


def _frame_dominante_azul():
    """Cena com forte dominante de cor — é o caso que expõe erro de composição de LUT
    (ordem errada de ganho/gamma chega a divergir 105/255 aqui)."""
    f = _frame(cor=(60, 70, 190), ruido=15)  # BGR: azul forte
    return f


def _frame_noite():
    return _frame(cor=(45, 40, 25), ruido=10)


_CFG_BASE = {
    "ajuste_ambiente": "sim", "ajuste_forca": "1.0", "ajuste_clahe": "sim",
    "ajuste_wb": "sim", "ajuste_saturacao": "sim", "ajuste_denoise_noite": "sim",
    "ajuste_recalc_frames": "8", "ajuste_brilho_alvo": "120",
}


def _cfg(**overrides):
    return {**_CFG_BASE, **overrides}


class TestEquivalenciaBitExata:
    """Caminhos que não passam por white-balance devem bater exatamente — CLAHE
    in-place e saturação por LUT são reescritas matematicamente idênticas."""

    @pytest.mark.parametrize("forca", ["1.0", "0.5"])
    def test_sem_wb_bate_exato(self, forca):
        cfg = _cfg(ajuste_wb="nao", ajuste_forca=forca)
        frame = _frame()
        ref, novo = _Referencia(cfg).processar(frame), AjustadorAmbiente(cfg).processar(frame)
        assert np.array_equal(ref, novo)

    def test_sem_wb_cena_noturna(self):
        """Ativa CLAHE com clip diferente + denoise (bilateralFilter) + saturação."""
        cfg = _cfg(ajuste_wb="nao")
        frame = _frame_noite()
        ref, novo = _Referencia(cfg).processar(frame), AjustadorAmbiente(cfg).processar(frame)
        assert np.array_equal(ref, novo)

    def test_desativado_devolve_o_mesmo_objeto(self):
        cfg = _cfg(ajuste_ambiente="nao")
        frame = _frame()
        assert AjustadorAmbiente(cfg).processar(frame) is frame


class TestEquivalenciaComWhiteBalance:
    """`_ganhos_wb` usa `mean(axis=(0,1), dtype=float32)` — mesma acumulação em
    float32 que a implementação anterior, só sem materializar a cópia do frame
    inteiro. A saída é BIT-A-BIT idêntica, não apenas "próxima": qualquer alternativa
    que mude a PRECISÃO da soma (cv2.mean, que acumula em double) foi descartada
    porque o CLAHE amplifica essa divergência de forma imprevisível (chegou a 43/255
    numa cena de teste) — ver o comentário em `_ganhos_wb`."""

    @pytest.mark.parametrize("gerador", [_frame, _frame_dominante_azul, _frame_noite])
    def test_bate_exato(self, gerador):
        cfg = _cfg()
        frame = gerador()
        ref = _Referencia(cfg).processar(frame)
        novo = AjustadorAmbiente(cfg).processar(frame)
        assert np.array_equal(ref, novo)

    @pytest.mark.parametrize("seed", range(10))
    @pytest.mark.parametrize("cor", [(120, 130, 110), (60, 70, 190), (200, 150, 50),
                                     (45, 40, 25), (10, 10, 10)])
    def test_bate_exato_em_varias_cenas_aleatorias(self, cor, seed):
        """A composição da LUT (ordem + truncagem) e a acumulação da média são o tipo
        de código que passa em 2-3 cenas de teste e falha na 4ª — por isso a
        variedade aqui, não só as 3 cenas fixas do teste acima."""
        cfg = _cfg()
        # `_frame` usa seed fixa (42); gera uma variante por combinação de cor/seed
        # para cobrir mais pontos do espaço sem reescrever o gerador.
        rng = np.random.default_rng(seed)
        frame = np.clip(np.array(cor, dtype=np.float32)
                        + rng.normal(0, 30, (720, 1280, 3)), 0, 255).astype(np.uint8)
        ref = _Referencia(cfg).processar(frame)
        novo = AjustadorAmbiente(cfg).processar(frame)
        assert np.array_equal(ref, novo)


class TestGanhosPorFrame:
    def test_ganhos_wb_sao_recalculados_a_cada_chamada_de_processar(self, monkeypatch):
        """Congelar os ganhos de white-balance no recalc (a cada `ajuste_recalc_frames`)
        faria a correção de cor reagir em degraus em vez de suavemente — o
        white-balance sempre foi recalculado a CADA frame, só o gamma/CLAHE/saturação
        é que ficam em cache. A otimização não pode mudar essa cadência: espiona o
        cálculo de ganhos e confirma que ele roda em toda chamada, não só nos recalcs.
        """
        cfg = _cfg(ajuste_recalc_frames="100")   # só o 1º frame recalcula o resto
        aj = AjustadorAmbiente(cfg)
        chamadas = []
        original = aj._ganhos_wb

        def _espiao(img):
            chamadas.append(1)
            return original(img)

        monkeypatch.setattr(aj, "_ganhos_wb", _espiao)
        frame = _frame()
        for _ in range(5):
            aj.processar(frame)
        assert len(chamadas) == 5, "ganhos de WB deveriam ser recalculados nas 5 chamadas"

    def test_dominante_de_cor_diferente_produz_ganhos_diferentes(self):
        """Confirma que o resultado do WB de fato reage à cena atual (não é um valor
        travado vindo de outro frame processado antes)."""
        cfg = _cfg(ajuste_recalc_frames="100")
        aj = AjustadorAmbiente(cfg)
        aj.processar(_frame(cor=(128, 128, 128), ruido=0))   # dispara o único recalc

        ganhos_azul = aj._ganhos_wb(_frame_dominante_azul())
        ganhos_vermelho = aj._ganhos_wb(_frame(cor=(30, 40, 200), ruido=0))
        assert ganhos_azul != pytest.approx(ganhos_vermelho, abs=0.05)


class TestRecalculoPeriodico:
    def test_recalcula_na_cadencia_esperada(self, monkeypatch):
        cfg = _cfg(ajuste_recalc_frames="3")
        aj = AjustadorAmbiente(cfg)
        chamadas = []
        original = aj._recalcular
        monkeypatch.setattr(aj, "_recalcular", lambda f: (chamadas.append(1), original(f)))
        frame = _frame()
        for _ in range(10):
            aj.processar(frame)
        # frames 1, 4, 7, 10 -> _frames % 3 == 1
        assert len(chamadas) == 4

    def test_registra_perfil_de_ambiente(self):
        cfg = _cfg()
        aj = AjustadorAmbiente(cfg, camera_db_id=7)
        aj.processar(_frame_noite())
        assert estado.ambiente[7]["perfil"] == "noite"


class TestRobustez:
    def test_frame_none(self):
        assert AjustadorAmbiente(_cfg()).processar(None) is None

    def test_frame_vazio(self):
        vazio = np.empty((0, 0, 3), dtype=np.uint8)
        assert AjustadorAmbiente(_cfg()).processar(vazio) is vazio

    def test_falha_interna_devolve_o_frame_original(self, monkeypatch):
        cfg = _cfg()
        aj = AjustadorAmbiente(cfg)
        monkeypatch.setattr(aj, "_recalcular", lambda f: (_ for _ in ()).throw(RuntimeError("boom")))
        frame = _frame()
        assert aj.processar(frame) is frame
