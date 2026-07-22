"""Ajuste adaptativo de imagem conforme a condição de ambiente (luz/contraste).

Analisa cada frame (em versão reduzida — barato) e classifica um "perfil de cena":

  - noite      : luminância muito baixa
  - baixa_luz  : luminância baixa (entardecer / interior escuro)
  - nublado    : luminância média mas contraste baixo (céu encoberto / chuva / neblina)
  - sol_forte  : luminância alta com muitos pixels estourados (ofuscamento)
  - normal     : condições equilibradas

Para cada perfil aplica correções calibradas, todas opcionais via config:

  - Gamma/brilho automático até uma luminância-alvo
  - CLAHE (contraste local adaptativo) — recupera detalhe em cena de baixo contraste
  - Balanço de branco gray-world — remove dominante de cor (azulado à noite, etc.)
  - Ajuste de saturação
  - Redução de ruído leve à noite

Notas de projeto:
  - A cena é reclassificada só a cada N frames; entre reclassificações os parâmetros
    (LUT de gamma, clip do CLAHE) ficam em cache, mantendo o custo por frame baixo.
  - Suavização temporal (EMA) no expoente de gamma evita oscilação frame a frame.
  - `forca` (0..1) mistura o frame corrigido com o original — um único botão de intensidade.
  - Qualquer exceção cai para o frame original: o ajuste NUNCA quebra o pipeline.

O detector de placa "chuva/tempestade" literal exigiria um modelo treinado ou uma API
de clima; aqui a ação corretiva dessas condições (baixo contraste, pouca luz) já é
coberta pela classificação por luminância/contraste.
"""
from __future__ import annotations
import logging

import cv2
import numpy as np

from app.core import estado

log = logging.getLogger(__name__)


def _bool(cfg: dict, chave: str, padrao: str = "nao") -> bool:
    return str(cfg.get(chave, padrao)).strip().lower() in ("sim", "true", "1", "yes")


def _float(cfg: dict, chave: str, padrao: float) -> float:
    try:
        return float(cfg.get(chave, padrao))
    except (TypeError, ValueError):
        return padrao


def _int(cfg: dict, chave: str, padrao: int) -> int:
    try:
        return int(float(cfg.get(chave, padrao)))
    except (TypeError, ValueError):
        return padrao


class AjustadorAmbiente:
    """Corrige um frame BGR de acordo com a condição de luz/contraste detectada."""

    def __init__(self, cfg: dict[str, str], camera_db_id: int = 0):
        self.camera_db_id = camera_db_id
        self.ativo = _bool(cfg, "ajuste_ambiente", "nao")
        self.brilho_alvo = min(220.0, max(60.0, _float(cfg, "ajuste_brilho_alvo", 120.0)))
        self.forca = min(1.0, max(0.0, _float(cfg, "ajuste_forca", 0.8)))
        self.usar_clahe = _bool(cfg, "ajuste_clahe", "sim")
        self.usar_wb = _bool(cfg, "ajuste_wb", "sim")
        self.usar_saturacao = _bool(cfg, "ajuste_saturacao", "sim")
        self.usar_denoise = _bool(cfg, "ajuste_denoise_noite", "sim")
        self.recalc_n = max(1, _int(cfg, "ajuste_recalc_frames", 8))

        # Estado adaptativo (persistente entre frames)
        self._frames = 0
        self._perfil = "normal"
        self._expo = 1.0            # expoente de gamma suavizado (EMA)
        self._lut: np.ndarray | None = None
        self._clahe = None
        self._clahe_clip = 0.0
        self._sat_factor = 1.0
        self._denoise = False

    # -- Análise ---------------------------------------------------------------

    def _stats(self, frame) -> dict:
        """Estatísticas de luz/contraste calculadas numa versão reduzida do frame."""
        h, w = frame.shape[:2]
        if w > 320:
            fator = 320.0 / w
            pequeno = cv2.resize(frame, (0, 0), fx=fator, fy=fator, interpolation=cv2.INTER_AREA)
        else:
            pequeno = frame
        cinza = cv2.cvtColor(pequeno, cv2.COLOR_BGR2GRAY)
        total = cinza.size
        lum = float(cinza.mean())
        contraste = float(cinza.std())
        pct_estourado = float(np.count_nonzero(cinza > 245)) / total
        pct_escuro = float(np.count_nonzero(cinza < 15)) / total
        sat = float(cv2.cvtColor(pequeno, cv2.COLOR_BGR2HSV)[..., 1].mean())
        return {
            "luminancia": lum,
            "contraste": contraste,
            "pct_estourado": pct_estourado,
            "pct_escuro": pct_escuro,
            "saturacao": sat,
        }

    @staticmethod
    def _classificar(s: dict) -> str:
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

    # -- Cálculo de parâmetros (a cada N frames) -------------------------------

    def _recalcular(self, frame) -> None:
        s = self._stats(frame)
        perfil = self._classificar(s)
        self._perfil = perfil

        # Expoente de gamma para levar a luminância média até o alvo.
        #   corrigido = 255 * (entrada/255) ** expo
        mean_norm = min(0.98, max(0.02, s["luminancia"] / 255.0))
        alvo_norm = min(0.98, max(0.05, self.brilho_alvo / 255.0))
        try:
            expo_alvo = np.log(alvo_norm) / np.log(mean_norm)
        except (ValueError, ZeroDivisionError):
            expo_alvo = 1.0
        expo_alvo = float(min(2.5, max(0.4, expo_alvo)))
        # Suavização temporal (EMA) para não piscar.
        self._expo = 0.7 * self._expo + 0.3 * expo_alvo
        self._lut = self._montar_lut(self._expo)

        # CLAHE: clip maior em cena de baixo contraste (nublado/chuva) e à noite.
        clip = {
            "nublado": 4.0,
            "noite": 3.0,
            "baixa_luz": 2.5,
            "sol_forte": 1.5,
            "normal": 2.0,
        }.get(perfil, 2.0)
        if self._clahe is None or abs(clip - self._clahe_clip) > 0.1:
            self._clahe = cv2.createCLAHE(clipLimit=clip, tileGridSize=(8, 8))
            self._clahe_clip = clip

        # Saturação: compensa dessaturação em pouca luz; segura em sol forte.
        self._sat_factor = {
            "noite": 1.15,
            "baixa_luz": 1.10,
            "nublado": 1.15,
            "sol_forte": 0.90,
            "normal": 1.0,
        }.get(perfil, 1.0)

        self._denoise = perfil in ("noite", "baixa_luz")

        estado.registrar_ambiente(
            self.camera_db_id, perfil,
            luminancia=round(s["luminancia"], 1),
            contraste=round(s["contraste"], 1),
            gamma=round(self._expo, 2),
        )

    @staticmethod
    def _montar_lut(expo: float) -> np.ndarray:
        x = np.arange(256, dtype=np.float32) / 255.0
        return np.clip((x ** expo) * 255.0, 0, 255).astype(np.uint8)

    # -- Correções por frame ---------------------------------------------------

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
    def _saturar(img, fator: float):
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).astype(np.float32)
        hsv[..., 1] = np.clip(hsv[..., 1] * fator, 0, 255)
        return cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)

    def processar(self, frame):
        """Retorna um NOVO frame BGR corrigido (não modifica o original in-place)."""
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
        except Exception as e:  # nunca deixa o ajuste derrubar o pipeline
            log.warning("AjustadorAmbiente: falha ao processar frame (%s) — usando original", e)
            return frame
