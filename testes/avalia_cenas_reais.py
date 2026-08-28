#!/usr/bin/env python3
"""Avalia a DETECÇÃO em cenas reais completas — o que `dataset.json` não mede.

`testes/dataset.json` é quase todo `tipo: crop` (placa já recortada), então
`run_testes.py` mede essencialmente o OCR: o detector nem chega a rodar. Este
script cobre o buraco usando os frames completos que o próprio sistema já salva
(`salvar_frame_deteccao=sim` → `*_frame.jpg` em app/web/static/snapshots/).

Esses frames contêm pessoas e placas reais de clientes, e o diretório é
gitignored de propósito — por isso este script LÊ do disco local e nunca copia
nada para dentro do repositório. Os rótulos ficam em `testes/cenas_reais.json`,
também gitignored.

Uso:
  python testes/avalia_cenas_reais.py                 # cobertura de detecção
  python testes/avalia_cenas_reais.py --ocr           # detecção + leitura
  python testes/avalia_cenas_reais.py --modelo-rotulos  # gera esqueleto de rótulos

Formato de testes/cenas_reais.json (opcional, para medir acerto de verdade):
  {"20260807T141134_QVH1067_frame.jpg": {"placas": ["QVH1D67", "HPY2371"]}}
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)

import cv2

SNAPSHOTS = Path("app/web/static/snapshots")
ROTULOS = Path("testes/cenas_reais.json")
MIN_L, MIN_A = 600, 400          # abaixo disso é recorte de placa, não cena


def _frames_reais() -> list[Path]:
    """Frames de cena completa ESTÁVEIS, salvos por `salvar_frame_deteccao`.

    O nome importa mais que o tamanho aqui. `preview_*.jpg` é sobrescrito a cada
    leitura do bico (leitura.py) e os artefatos de debug do pipeline
    (`0_frame_bbox.jpg`, `diag_fluxo.jpg`) também rodam — incluí-los faria o
    conjunto de avaliação mudar entre execuções, e comparar duas medições deixaria
    de significar alguma coisa. Só entram os `<timestamp>_<placa>_frame.jpg`, que
    têm nome único e persistem.

    Filtrar por nome antes de decodificar também evita abrir centenas de recortes
    de placa só para descobrir o tamanho.
    """
    achados = []
    for p in sorted(SNAPSHOTS.glob("*_frame.jpg")):
        if p.name.startswith("preview_"):
            continue
        img = cv2.imread(str(p))
        if img is None:
            continue
        h, w = img.shape[:2]
        if w >= MIN_L and h >= MIN_A:
            achados.append(p)
    return achados


def _detectores(cfg: dict):
    """Os dois caminhos de produção, pelas FÁBRICAS de produção.

    Nada é remontado à mão aqui de propósito. A versão anterior instanciava
    `DetectorDoisEstagios` diretamente para o caminho GET e, com isso, deixava de fora o
    `BuscaEmTiles` que `obter_detector_leitura` põe por cima quando `tiles_fallback_get`
    está ligado (o padrão) — justamente a varredura em janelas que é a correção medida
    para placa de moto (0/12 → 12/12). O script existe para responder "produção detecta
    isto?", e media um detector mais fraco que produção.
    """
    from app.visao.detector import criar_detector, obter_detector_leitura
    live = criar_detector(cfg)
    live.carregar()
    get = obter_detector_leitura(cfg)   # já vem carregado pela fábrica
    return live, get


def main() -> None:
    ap = argparse.ArgumentParser(description="Mede detecção em cenas reais completas")
    ap.add_argument("--ocr", action="store_true", help="também roda OCR nas placas detectadas")
    ap.add_argument("--modelo-rotulos", action="store_true",
                    help="gera esqueleto de testes/cenas_reais.json e sai")
    args = ap.parse_args()

    frames = _frames_reais()
    if not frames:
        print(f"Nenhum frame de cena completa em {SNAPSHOTS}/ (>= {MIN_L}x{MIN_A}).")
        print("Ative salvar_frame_deteccao=sim e rode algumas leituras primeiro.")
        return

    if args.modelo_rotulos:
        molde = {p.name: {"placas": []} for p in frames}
        ROTULOS.write_text(json.dumps(molde, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Esqueleto com {len(molde)} frames em {ROTULOS}")
        print("Preencha 'placas' com as placas visíveis em cada cena e rode de novo.")
        return

    rotulos = {}
    if ROTULOS.exists():
        rotulos = {k: v for k, v in json.loads(ROTULOS.read_text(encoding="utf-8")).items()
                   if v.get("placas")}

    from app.core import config as cfg_mod
    cfg = cfg_mod.carregar()
    live, get = _detectores(cfg)

    ocr = None
    if args.ocr:
        from app.visao.ocr import AutoOCR
        ocr = AutoOCR(tesseract_psm=int(cfg.get("tesseract_psm", "7")))
        ocr.carregar()
        from app.visao.pipeline import _expandir_bbox
        from app.visao.validador import validar

    print(f"\n{'='*72}")
    print(f"  DETECÇÃO EM CENAS REAIS — {len(frames)} frames"
          + (f", {len(rotulos)} rotulados" if rotulos else ", sem rótulos"))
    print(f"{'='*72}\n")

    n_live = n_get = 0
    regressoes = []
    lidas_ok = lidas_tot = 0

    for p in frames:
        img = cv2.imread(str(p))
        b_live = live.detectar(img)
        b_get = get.detectar(img)
        n_live += len(b_live)
        n_get += len(b_get)
        if len(b_live) > len(b_get):
            regressoes.append((p.name, len(b_live), len(b_get)))

        if ocr is None:
            continue
        esperadas = {s.upper() for s in rotulos.get(p.name, {}).get("placas", [])}
        if not esperadas:
            continue
        f_h, f_w = img.shape[:2]
        lidas = set()
        for x, y, w, h, _c in b_get:
            xe, ye, we, he = _expandir_bbox(x, y, w, h, f_w, f_h)
            crop = img[ye:ye + he, xe:xe + we]
            if crop.size == 0:
                continue
            texto, _ = ocr.ler(crop)
            val = validar(texto)
            if val:
                lidas.add(val[0])
        acertos = esperadas & lidas
        lidas_ok += len(acertos)
        lidas_tot += len(esperadas)
        faltou = esperadas - lidas
        marca = "OK " if not faltou else "ERR"
        print(f"  {marca} {p.name[:40]:42s} esperado={sorted(esperadas)} lido={sorted(lidas)}")

    print(f"\n  Placas detectadas — 1 estágio (live): {n_live}   2 estágios (GET): {n_get}")
    if regressoes:
        print(f"\n  ATENÇÃO: 2 estágios detectou MENOS que 1 estágio em {len(regressoes)}/{len(frames)} frames.")
        print("  (o filtro de veículo achou algum veículo, mas não o que tinha a placa)")
        for nome, a, b in regressoes:
            print(f"    {nome[:44]:46s} live={a}  get={b}")
    if lidas_tot:
        print(f"\n  LEITURA (nos frames rotulados): {lidas_ok}/{lidas_tot} "
              f"({lidas_ok/lidas_tot*100:.1f}%)")
        print("  ATENÇÃO: isto é leitura de FRAME ÚNICO. Em produção o loop de leitura")
        print("  (leitura.py) tira até leitura_max_tentativas fotos e faz consenso por")
        print("  caractere, o que corrige boa parte destes erros. Use este número para")
        print("  comparar mudanças entre si, não como a acurácia final do sistema.")
    if not rotulos:
        print(f"\n  Sem rótulos: só a contagem de detecções é confiável aqui.")
        print(f"  Rode com --modelo-rotulos para criar {ROTULOS} e medir acerto real.")


if __name__ == "__main__":
    main()
