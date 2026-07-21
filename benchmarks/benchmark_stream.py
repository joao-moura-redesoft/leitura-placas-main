"""
Benchmark: MJPEG vs HLS (simulado) - custo real de CPU e banda por protocolo.

Execucao:
    python benchmark_stream.py

Nao requer servidor rodando. Usa frames sinteticos (1280x720).
"""
from __future__ import annotations
import io
import os
import statistics
import sys
import threading
import time

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import cv2
import numpy as np
import psutil

LARGURA, ALTURA = 1280, 720
QUALIDADE_JPEG = 75
FPS_ALVO = 15
DURACAO_SEG = 5
PROCESS = psutil.Process(os.getpid())


def _frame_sintetico(i: int) -> np.ndarray:
    frame = np.zeros((ALTURA, LARGURA, 3), dtype=np.uint8)
    frame[:] = (30, 60 + (i % 40), 90)
    cv2.putText(frame, f"Frame {i}", (50, ALTURA // 2),
                cv2.FONT_HERSHEY_SIMPLEX, 3, (255, 255, 255), 4)
    return frame


# ---------------------------------------------------------------------------
# Cenario 1: MJPEG - custo por viewer (como esta hoje)
# ---------------------------------------------------------------------------

def _simular_viewer_mjpeg(resultados: list, viewer_id: int, duracao: float) -> None:
    bytes_total = 0
    frames_total = 0
    intervalo = 1.0 / FPS_ALVO
    t_fim = time.time() + duracao
    i = 0
    while time.time() < t_fim:
        t0 = time.time()
        frame = _frame_sintetico(i)
        ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, QUALIDADE_JPEG])
        if ok:
            bytes_total += len(jpg.tobytes())
            frames_total += 1
        restante = intervalo - (time.time() - t0)
        if restante > 0:
            time.sleep(restante)
        i += 1
    resultados.append({"viewer": viewer_id, "bytes": bytes_total, "frames": frames_total})


def bench_mjpeg(n_viewers: int) -> dict:
    resultados: list[dict] = []
    threads: list[threading.Thread] = []
    time.sleep(0.1)
    PROCESS.cpu_percent(interval=None)  # descarta leitura inicial
    mem_antes = PROCESS.memory_info().rss / 1024 / 1024

    t0 = time.time()
    for v in range(n_viewers):
        t = threading.Thread(target=_simular_viewer_mjpeg,
                             args=(resultados, v, DURACAO_SEG), daemon=True)
        threads.append(t)
        t.start()

    amostras_cpu: list[float] = []
    while any(t.is_alive() for t in threads):
        amostras_cpu.append(PROCESS.cpu_percent(interval=0.5))
    for t in threads:
        t.join()

    elapsed = time.time() - t0
    mem_depois = PROCESS.memory_info().rss / 1024 / 1024
    total_bytes = sum(r["bytes"] for r in resultados)
    total_frames = sum(r["frames"] for r in resultados)
    fps_real = total_frames / n_viewers / elapsed

    return {
        "viewers": n_viewers,
        "fps_medio": fps_real,
        "cpu_medio_pct": statistics.mean(amostras_cpu) if amostras_cpu else 0,
        "cpu_pico_pct": max(amostras_cpu) if amostras_cpu else 0,
        "banda_mbps": (total_bytes * 8) / elapsed / 1_000_000,
        "banda_por_viewer_mbps": (total_bytes * 8) / elapsed / 1_000_000 / n_viewers,
        "bytes_por_frame_kb": (total_bytes / total_frames / 1024) if total_frames else 0,
        "ram_delta_mb": mem_depois - mem_antes,
    }


# ---------------------------------------------------------------------------
# Cenario 2: HLS simulado - encode 1x, serve N viewers
# ---------------------------------------------------------------------------

def bench_hls_simulado(n_viewers: int) -> dict:
    """HLS: encode ocorre 1 vez por camera; viewers so leem bytes do buffer."""
    segmentos_prontos: list[bytes] = []
    t0_enc = time.time()

    for i in range(FPS_ALVO * DURACAO_SEG):
        frame = _frame_sintetico(i)
        ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, QUALIDADE_JPEG])
        if ok:
            segmentos_prontos.append(jpg.tobytes())

    t_encode = time.time() - t0_enc
    bytes_encode = sum(len(s) for s in segmentos_prontos)

    bytes_servidos = [0] * n_viewers

    def _viewer_hls(idx: int) -> None:
        for seg in segmentos_prontos:
            bytes_servidos[idx] += len(seg)
            time.sleep(1.0 / FPS_ALVO * 0.1)  # simula latencia de I/O de rede

    PROCESS.cpu_percent(interval=None)
    t0_serve = time.time()
    threads = [threading.Thread(target=_viewer_hls, args=(v,), daemon=True)
               for v in range(n_viewers)]
    amostras_cpu: list[float] = []
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        amostras_cpu.append(PROCESS.cpu_percent(interval=0.3))
    for t in threads:
        t.join()

    elapsed_serve = time.time() - t0_serve
    total_bytes_servidos = sum(bytes_servidos)

    return {
        "viewers": n_viewers,
        "fps_medio": FPS_ALVO,
        "cpu_encode_seg": t_encode,
        "cpu_medio_pct": statistics.mean(amostras_cpu) if amostras_cpu else 0,
        "cpu_pico_pct": max(amostras_cpu) if amostras_cpu else 0,
        "banda_mbps": (total_bytes_servidos * 8) / elapsed_serve / 1_000_000,
        "banda_por_viewer_mbps": (total_bytes_servidos * 8) / elapsed_serve / 1_000_000 / n_viewers,
        "bytes_por_frame_kb": bytes_encode / len(segmentos_prontos) / 1024 if segmentos_prontos else 0,
    }


# ---------------------------------------------------------------------------
# Cenario 3: escala de cameras (N cameras x 1 viewer cada)
# ---------------------------------------------------------------------------

def bench_escala_cameras(n_cameras: int) -> dict:
    resultados: list[dict] = []
    threads: list[threading.Thread] = []
    PROCESS.cpu_percent(interval=None)

    for c in range(n_cameras):
        t = threading.Thread(target=_simular_viewer_mjpeg,
                             args=(resultados, c, DURACAO_SEG), daemon=True)
        threads.append(t)
        t.start()

    amostras_cpu: list[float] = []
    while any(t.is_alive() for t in threads):
        amostras_cpu.append(PROCESS.cpu_percent(interval=0.5))
    for t in threads:
        t.join()

    total_bytes = sum(r["bytes"] for r in resultados)

    return {
        "cameras": n_cameras,
        "cpu_medio_pct": statistics.mean(amostras_cpu) if amostras_cpu else 0,
        "cpu_pico_pct": max(amostras_cpu) if amostras_cpu else 0,
        "banda_total_mbps": (total_bytes * 8) / DURACAO_SEG / 1_000_000,
        "banda_por_camera_mbps": (total_bytes * 8) / DURACAO_SEG / 1_000_000 / n_cameras,
    }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _l(label: str, valor: str, unidade: str = "") -> None:
    print(f"  {label:<35} {valor:>10} {unidade}")


def main() -> None:
    print()
    print("=" * 64)
    print("  BENCHMARK: MJPEG vs HLS - Streaming de Cameras")
    print(f"  Resolucao: {LARGURA}x{ALTURA}  |  Qualidade: {QUALIDADE_JPEG}%  |  FPS alvo: {FPS_ALVO}")
    print("=" * 64)

    # -- Teste 1: MJPEG com N viewers -----------------------------------------
    print()
    print("--- MJPEG: custo por numero de viewers (1 camera) ---")
    for n in [1, 2, 4, 8]:
        r = bench_mjpeg(n)
        print(f"\n  {n} viewer(s):")
        _l("    FPS entregue",        f"{r['fps_medio']:.1f}",              "fps")
        _l("    CPU processo",        f"{r['cpu_medio_pct']:.1f}",          "% (medio)")
        _l("    CPU pico",            f"{r['cpu_pico_pct']:.1f}",           "%")
        _l("    Banda total",         f"{r['banda_mbps']:.2f}",             "Mbps")
        _l("    Banda por viewer",    f"{r['banda_por_viewer_mbps']:.2f}",  "Mbps")
        _l("    Tamanho medio frame", f"{r['bytes_por_frame_kb']:.1f}",     "KB")
        _l("    RAM delta",           f"{r['ram_delta_mb']:.1f}",           "MB")

    # -- Teste 2: HLS simulado com N viewers ----------------------------------
    print()
    print("\n--- HLS (encode 1x, serve N viewers) ---")
    for n in [1, 2, 4, 8]:
        r = bench_hls_simulado(n)
        print(f"\n  {n} viewer(s):")
        _l("    FPS entregue",        f"{r['fps_medio']:.1f}",             "fps")
        _l("    CPU encode (1x)",     f"{r['cpu_encode_seg']*1000:.0f}",   "ms total")
        _l("    CPU serve (medio)",   f"{r['cpu_medio_pct']:.1f}",         "% (so I/O)")
        _l("    Banda total",         f"{r['banda_mbps']:.2f}",            "Mbps")
        _l("    Banda por viewer",    f"{r['banda_por_viewer_mbps']:.2f}", "Mbps")

    # -- Teste 3: escala de cameras MJPEG ------------------------------------
    print()
    print("\n--- MJPEG: escala de cameras (1 viewer por camera) ---")
    for n in [1, 2, 4, 8, 12, 16]:
        r = bench_escala_cameras(n)
        print(f"\n  {n:>2} camera(s):")
        _l("    CPU medio",          f"{r['cpu_medio_pct']:.1f}",              "%")
        _l("    CPU pico",           f"{r['cpu_pico_pct']:.1f}",               "%")
        _l("    Banda total",        f"{r['banda_total_mbps']:.2f}",           "Mbps")
        _l("    Banda por camera",   f"{r['banda_por_camera_mbps']:.2f}",      "Mbps")

    # -- Resumo comparativo --------------------------------------------------
    print()
    print("\n" + "=" * 64)
    print("  RESUMO COMPARATIVO")
    print("=" * 64)
    r1 = bench_mjpeg(1)
    r8 = bench_mjpeg(8)
    h1 = bench_hls_simulado(1)
    h8 = bench_hls_simulado(8)

    cpu_mult = r8['cpu_medio_pct'] / max(r1['cpu_medio_pct'], 0.1)
    bnd_mult = r8['banda_mbps'] / max(r1['banda_mbps'], 0.01)

    print(f"\n  MJPEG 1 viewer:   {r1['cpu_medio_pct']:5.1f}% CPU   {r1['banda_mbps']:.2f} Mbps")
    print(f"  MJPEG 8 viewers:  {r8['cpu_medio_pct']:5.1f}% CPU   {r8['banda_mbps']:.2f} Mbps"
          f"  (x{cpu_mult:.1f} CPU, x{bnd_mult:.1f} banda)")
    print(f"\n  HLS   1 viewer:   {h1['cpu_medio_pct']:5.1f}% CPU   {h1['banda_mbps']:.2f} Mbps  (encode fixo: {h1['cpu_encode_seg']*1000:.0f}ms)")
    print(f"  HLS   8 viewers:  {h8['cpu_medio_pct']:5.1f}% CPU   {h8['banda_mbps']:.2f} Mbps  (CPU nao escala - encode e 1x)")
    print()
    print("  NOTA: benchmark mede custo de encode+serve.")
    print("  YOLO/OCR nao esta incluido - e separado e dominante.")
    print()


if __name__ == "__main__":
    try:
        import psutil  # noqa: F401
    except ImportError:
        print("Instale: pip install psutil")
        sys.exit(1)
    main()
