"""
Benchmark: Modo Classico vs Tracker (IoU/ByteTrack)

Simula cenarios reais de posto/logistica sem precisar de camera ou YOLO real.
Mede: chamadas OCR, CPU, latencia por frame, reducao de carga.

Execucao:
    python benchmark_tracker.py
"""
from __future__ import annotations
import io
import os
import statistics
import sys
import threading
import time
from collections import defaultdict

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import numpy as np
import psutil

PROCESS = psutil.Process(os.getpid())

# ── Parametros do benchmark ───────────────────────────────────────────────────

FPS_DETECCAO  = 5       # deteccoes por segundo (config deteccao_fps_max)
OCR_LATENCIA  = 0.045   # 45ms por chamada OCR (realista para EasyOCR CPU)
YOLO_LATENCIA = 0.012   # 12ms por frame YOLO (ONNX CPU, placa_detector.onnx)
FRAME_W, FRAME_H = 1280, 720

# Cenarios: lista de (nome, veiculos) onde cada veiculo = (placa, t_entrada, t_saida, bbox)
CENARIOS = {
    "Posto simples (1 camera, 1 veiculo por vez)": [
        ("ABC1D23", 0, 90, (200, 150, 300, 80)),   # caminhao para 90s
    ],
    "Posto movimentado (4 veiculos simultaneos)": [
        ("ABC1D23", 0,  60, (100, 100, 280, 80)),
        ("XYZ9W87", 5,  55, (400, 100, 280, 80)),
        ("QWE5R67", 10, 70, (700, 100, 280, 80)),
        ("MNO3P45", 0,  80, (100, 400, 280, 80)),
    ],
    "Centro logistico (8 veiculos, rotatividade alta)": [
        ("TRK1A11", 0,  120, (50,  50,  280, 80)),
        ("TRK2B22", 10, 90,  (380, 50,  280, 80)),
        ("TRK3C33", 5,  110, (710, 50,  280, 80)),
        ("TRK4D44", 0,  60,  (50,  300, 280, 80)),
        ("TRK5E55", 20, 100, (380, 300, 280, 80)),
        ("TRK6F66", 15, 85,  (710, 300, 280, 80)),
        ("TRK7G77", 30, 70,  (50,  550, 280, 80)),
        ("TRK8H88", 25, 95,  (380, 550, 280, 80)),
    ],
}


# ── Mocks ─────────────────────────────────────────────────────────────────────

class MockOCR:
    """OCR simulado: conta chamadas e simula latencia realista."""
    def __init__(self, latencia: float = OCR_LATENCIA):
        self.chamadas = 0
        self._latencia = latencia
        self.lock = threading.Lock()

    def ler(self, crop) -> tuple[str, float]:
        with self.lock:
            self.chamadas += 1
        time.sleep(self._latencia)
        return "ABC1D23", 0.92  # sempre retorna placa valida

    def reset(self):
        with self.lock:
            self.chamadas = 0


class MockDetector:
    """YOLO simulado: retorna bboxes dos veiculos ativos no instante t."""
    def __init__(self, latencia: float = YOLO_LATENCIA):
        self._latencia = latencia

    def detectar(self, bboxes_ativos: list) -> list:
        time.sleep(self._latencia)
        return [(x, y, w, h, 0.92) for x, y, w, h in bboxes_ativos]


def _frame_sintetico() -> np.ndarray:
    return np.zeros((FRAME_H, FRAME_W, 3), dtype=np.uint8)


# ── Logica de simulacao ───────────────────────────────────────────────────────

def _bboxes_no_instante(veiculos: list, t: float) -> list:
    """Retorna bboxes dos veiculos presentes no instante t."""
    return [
        (x, y, w, h)
        for _, t_in, t_out, (x, y, w, h) in veiculos
        if t_in <= t <= t_out
    ]


def simular_classico(veiculos: list, ocr: MockOCR, det: MockDetector,
                     frames_consenso: int = 3, cooldown_seg: int = 120) -> dict:
    """
    Modo classico: OCR em todo bbox detectado a cada frame de deteccao.
    Emite apos frames_consenso leituras iguais consecutivas.
    """
    duracao = max(t_out for _, _, t_out, _ in veiculos)
    n_frames = int(duracao * FPS_DETECCAO)
    historico = defaultdict(list)  # placa → lista de leituras recentes
    ultima_emissao: dict[str, float] = {}
    emissoes = 0
    latencias = []

    ocr.reset()
    PROCESS.cpu_percent(interval=None)
    # -cooldown_seg garante que a primeira emissão sempre passa (igual ao código real)
    ultima_emissao_default = -cooldown_seg

    for i in range(n_frames):
        t = i / FPS_DETECCAO
        frame = _frame_sintetico()
        t0 = time.time()

        bboxes_ativos = _bboxes_no_instante(veiculos, t)
        bboxes = det.detectar(bboxes_ativos)

        for x, y, w, h, _ in bboxes:
            crop = frame[y:y+h, x:x+w] if y+h <= FRAME_H and x+w <= FRAME_W else frame[:80, :280]
            texto, _ = ocr.ler(crop)

            historico[texto].append(texto)
            recentes = historico[texto][-frames_consenso:]
            if len(recentes) >= frames_consenso and len(set(recentes)) == 1:
                if t - ultima_emissao.get(texto, ultima_emissao_default) >= cooldown_seg:
                    ultima_emissao[texto] = t
                    emissoes += 1
                    historico[texto].clear()

        latencias.append(time.time() - t0)

    amostras_cpu = [PROCESS.cpu_percent(interval=0.1) for _ in range(5)]
    return {
        "ocr_chamadas": ocr.chamadas,
        "emissoes":     emissoes,
        "latencia_med": statistics.mean(latencias) * 1000,
        "latencia_max": max(latencias) * 1000,
        "cpu_med":      statistics.mean(amostras_cpu),
        "n_frames":     n_frames,
    }


def simular_tracker(veiculos: list, ocr: MockOCR, det: MockDetector,
                    ocr_intervalo: int = 5, votos_emitir: int = 2,
                    cooldown_seg: int = 120) -> dict:
    """
    Modo tracker: OCR apenas no primeiro frame e a cada ocr_intervalo frames por track.
    Emite apos votos_emitir leituras concordantes do mesmo track.
    """
    from tracker import Tracker

    duracao = max(t_out for _, _, t_out, _ in veiculos)
    n_frames = int(duracao * FPS_DETECCAO)
    ultima_emissao: dict[str, float] = {}
    emissoes = 0
    latencias = []

    tr = Tracker(ocr_a_cada_n_frames=ocr_intervalo, votos_emitir=votos_emitir)
    tr.carregar()
    ocr.reset()
    PROCESS.cpu_percent(interval=None)

    for i in range(n_frames):
        t = i / FPS_DETECCAO
        frame = _frame_sintetico()
        t0 = time.time()

        bboxes_ativos = _bboxes_no_instante(veiculos, t)
        bboxes = det.detectar(bboxes_ativos)

        tracks = tr.update(bboxes, frame)

        for x, y, w, h, conf_det, track_id in tracks:
            if tr.precisa_ocr(track_id):
                crop = frame[y:y+h, x:x+w] if y+h <= FRAME_H and x+w <= FRAME_W else frame[:80, :280]
                texto, conf_ocr = ocr.ler(crop)
                tr.registrar_ocr(track_id, texto, "mercosul", (conf_det + conf_ocr) / 2)

            pronto = tr.placa_pronta(track_id)
            if pronto:
                placa, _, _ = pronto
                if t - ultima_emissao.get(placa, -cooldown_seg) >= cooldown_seg:
                    ultima_emissao[placa] = t
                    emissoes += 1
                    tr.marcar_emitido(track_id)

        latencias.append(time.time() - t0)

    amostras_cpu = [PROCESS.cpu_percent(interval=0.1) for _ in range(5)]
    return {
        "ocr_chamadas": ocr.chamadas,
        "emissoes":     emissoes,
        "latencia_med": statistics.mean(latencias) * 1000,
        "latencia_max": max(latencias) * 1000,
        "cpu_med":      statistics.mean(amostras_cpu),
        "n_frames":     n_frames,
    }


# ── Runner ────────────────────────────────────────────────────────────────────

def _l(label: str, valor: str, unidade: str = "") -> None:
    print(f"  {label:<40} {valor:>10} {unidade}")


def main() -> None:
    ocr = MockOCR(latencia=OCR_LATENCIA)
    det = MockDetector(latencia=YOLO_LATENCIA)

    print()
    print("=" * 68)
    print("  BENCHMARK: Modo Classico vs Tracker (IoU)")
    print(f"  OCR simulado: {OCR_LATENCIA*1000:.0f}ms/chamada | "
          f"YOLO: {YOLO_LATENCIA*1000:.0f}ms/frame | "
          f"Deteccao: {FPS_DETECCAO}fps")
    print("=" * 68)

    totais_classico = defaultdict(float)
    totais_tracker = defaultdict(float)

    for nome, veiculos in CENARIOS.items():
        duracao = max(t_out for _, _, t_out, _ in veiculos)
        n_veiculos = len(veiculos)

        print(f"\n{'─'*68}")
        print(f"  CENARIO: {nome}")
        print(f"  {n_veiculos} veiculo(s) | duracao simulada: {duracao}s | "
              f"{int(duracao * FPS_DETECCAO)} frames de deteccao")
        print(f"{'─'*68}")

        rc = simular_classico(veiculos, ocr, det)
        rt = simular_tracker(veiculos, ocr, det)

        reducao_ocr = (1 - rt["ocr_chamadas"] / max(rc["ocr_chamadas"], 1)) * 100
        reducao_lat = (1 - rt["latencia_med"] / max(rc["latencia_med"], 0.001)) * 100
        reducao_cpu = (1 - rt["cpu_med"] / max(rc["cpu_med"], 0.1)) * 100

        print(f"\n  {'':40} {'CLASSICO':>10}  {'TRACKER':>10}  {'REDUCAO':>10}")
        print(f"  {'-'*66}")
        _l("Chamadas OCR totais",
           f"{rc['ocr_chamadas']:>10}",
           f"-> {rt['ocr_chamadas']:>4}   {reducao_ocr:>5.1f}% menos")
        _l("Deteccoes emitidas",
           f"{rc['emissoes']:>10}",
           f"-> {rt['emissoes']:>4}")
        _l("Latencia media por frame (ms)",
           f"{rc['latencia_med']:>9.1f}",
           f"-> {rt['latencia_med']:>7.1f}  {reducao_lat:>5.1f}% menos")
        _l("Latencia maxima por frame (ms)",
           f"{rc['latencia_max']:>9.1f}",
           f"-> {rt['latencia_max']:>7.1f}")
        _l("CPU processo (med%)",
           f"{rc['cpu_med']:>9.1f}",
           f"-> {rt['cpu_med']:>7.1f}  {reducao_cpu:>5.1f}% menos")

        for k in ("ocr_chamadas", "latencia_med", "cpu_med"):
            totais_classico[k] += rc[k]
            totais_tracker[k] += rt[k]

    # Resumo geral
    n = len(CENARIOS)
    print(f"\n{'='*68}")
    print("  RESUMO GERAL (media dos cenarios)")
    print(f"{'='*68}")
    med_ocr_c  = totais_classico["ocr_chamadas"] / n
    med_ocr_t  = totais_tracker["ocr_chamadas"] / n
    med_lat_c  = totais_classico["latencia_med"] / n
    med_lat_t  = totais_tracker["latencia_med"] / n
    med_cpu_c  = totais_classico["cpu_med"] / n
    med_cpu_t  = totais_tracker["cpu_med"] / n

    print(f"\n  {'':40} {'CLASSICO':>10}  {'TRACKER':>10}  {'GANHO':>10}")
    print(f"  {'-'*66}")
    _l("Media OCR/cenario",
       f"{med_ocr_c:>10.0f}",
       f"-> {med_ocr_t:>6.0f}   {(1-med_ocr_t/max(med_ocr_c,1))*100:>5.1f}% menos")
    _l("Latencia media (ms)",
       f"{med_lat_c:>10.1f}",
       f"-> {med_lat_t:>8.1f}  {(1-med_lat_t/max(med_lat_c,0.001))*100:>5.1f}% menos")
    _l("CPU medio (%)",
       f"{med_cpu_c:>10.1f}",
       f"-> {med_cpu_t:>8.1f}  {(1-med_cpu_t/max(med_cpu_c,0.1))*100:>5.1f}% menos")

    print()
    print("  NOTAS:")
    print("  - OCR simulado com latencia fixa (EasyOCR CPU ~45ms)")
    print("  - YOLO simulado (ONNX CPU ~12ms) — roda em TODOS os frames em ambos os modos")
    print("  - Tracker IoU interno (boxmot nao instalado)")
    print("  - cooldown_seg=120 — mesmo veiculo nao re-emite em 2 minutos")
    print()


if __name__ == "__main__":
    try:
        import psutil  # noqa: F401
    except ImportError:
        print("Instale: pip install psutil")
        sys.exit(1)
    main()
