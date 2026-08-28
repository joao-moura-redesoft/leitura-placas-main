#!/usr/bin/env python3
"""
Gera imagens sintéticas de placas brasileiras.

NÃO USE PARA MEDIR ACURÁCIA. Em 12/08/2026 as 36 sintéticas foram removidas do dataset
porque não inflavam o número — invertiam o sinal. Com elas o ensemble com PaddleOCR
media 95,2% contra 90,5% do AutoOCR; só com fotos reais a ordem inverte (4/5 contra 5/5).
Placa sintética é nítida, frontal e em alta resolução: ela mede a fonte, não o problema.

Continua útil para checar o VALIDADOR e casos de caractere ambíguo (O/0, I/1, Q/O), onde
saber o gabarito por construção é justamente a vantagem. Se gerar imagens, mantenha-as
fora de `testes/dataset.json`.
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from PIL import Image, ImageDraw, ImageFont
import numpy as np

FOTOS_DIR = ROOT / "testes" / "fotos"
FOTOS_DIR.mkdir(parents=True, exist_ok=True)

FONT_PATH = "C:/Windows/Fonts/arialbd.ttf"  # Arial Bold

# ---------------------------------------------------------------------------
# Definição das placas a gerar
# ---------------------------------------------------------------------------
PLACAS = {
    "mercosul_carro": [
        "ABC1D23",
        "XYZ9E87",
        "QRS4F56",
        "DEF7G89",
        "GHI2J34",
    ],
    "mercosul_moto": [
        "JKL3M45",
        "NOP5Q67",
        "RST6U78",
        "VWX8Y90",
        "BCD0E12",
    ],
    "antigo_carro": [
        "FBI5551",
        "MNO2345",
        "PQR6789",
        "STU1234",
        "VWX8901",
    ],
    "antigo_moto": [
        "YZA3456",
        "BCD7890",
        "EFG1234",
        "HIJ5678",
        "KLM9012",
    ],
}


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_PATH, size)


def gerar_mercosul_carro(placa: str) -> Image.Image:
    """
    400x130 px
    Faixa azul superior 25px com "BRASIL" centralizado branco
    Fundo branco, texto preto centralizado
    """
    W, H = 400, 130
    FAIXA_H = 25
    AZUL = (0, 51, 153)
    BRANCO = (255, 255, 255)
    PRETO = (0, 0, 0)
    BORDA = (0, 0, 0)

    img = Image.new("RGB", (W, H), BRANCO)
    draw = ImageDraw.Draw(img)

    # Borda externa
    draw.rectangle([0, 0, W - 1, H - 1], outline=BORDA, width=4)

    # Faixa azul superior
    draw.rectangle([0, 0, W, FAIXA_H], fill=AZUL)

    # "BRASIL" na faixa
    fnt_brasil = _font(15)
    bb = draw.textbbox((0, 0), "BRASIL", font=fnt_brasil)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    draw.text(((W - tw) // 2, (FAIXA_H - th) // 2 - 1), "BRASIL", font=fnt_brasil, fill=BRANCO)

    # Texto da placa
    fnt_placa = _font(58)
    bb2 = draw.textbbox((0, 0), placa, font=fnt_placa)
    tw2, th2 = bb2[2] - bb2[0], bb2[3] - bb2[1]
    area_h = H - FAIXA_H
    tx = (W - tw2) // 2
    ty = FAIXA_H + (area_h - th2) // 2 - 2
    draw.text((tx, ty), placa, font=fnt_placa, fill=PRETO)

    return img


def gerar_mercosul_moto(placa: str) -> Image.Image:
    """
    200x140 px
    Faixa azul superior 30px com "BRASIL"
    Fundo branco, texto preto em duas linhas (AAA / 0A00)
    """
    W, H = 200, 140
    FAIXA_H = 30
    AZUL = (0, 51, 153)
    BRANCO = (255, 255, 255)
    PRETO = (0, 0, 0)

    img = Image.new("RGB", (W, H), BRANCO)
    draw = ImageDraw.Draw(img)

    draw.rectangle([0, 0, W - 1, H - 1], outline=PRETO, width=3)

    # Faixa azul
    draw.rectangle([0, 0, W, FAIXA_H], fill=AZUL)

    fnt_brasil = _font(13)
    bb = draw.textbbox((0, 0), "BRASIL", font=fnt_brasil)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    draw.text(((W - tw) // 2, (FAIXA_H - th) // 2 - 1), "BRASIL", font=fnt_brasil, fill=BRANCO)

    # Placa moto: 3 letras em cima, 4 chars embaixo
    linha1 = placa[:3]
    linha2 = placa[3:]

    area_h = H - FAIXA_H
    fnt_placa = _font(36)

    bb1 = draw.textbbox((0, 0), linha1, font=fnt_placa)
    tw1, th1 = bb1[2] - bb1[0], bb1[3] - bb1[1]
    bb2 = draw.textbbox((0, 0), linha2, font=fnt_placa)
    tw2, th2 = bb2[2] - bb2[0], bb2[3] - bb2[1]

    total_text_h = th1 + 6 + th2
    start_y = FAIXA_H + (area_h - total_text_h) // 2

    draw.text(((W - tw1) // 2, start_y), linha1, font=fnt_placa, fill=PRETO)
    draw.text(((W - tw2) // 2, start_y + th1 + 6), linha2, font=fnt_placa, fill=PRETO)

    return img


def gerar_antigo_carro(placa: str) -> Image.Image:
    """
    400x130 px
    Fundo branco puro, sem faixa
    Texto preto centralizado
    """
    W, H = 400, 130
    BRANCO = (255, 255, 255)
    PRETO = (0, 0, 0)

    img = Image.new("RGB", (W, H), BRANCO)
    draw = ImageDraw.Draw(img)

    draw.rectangle([0, 0, W - 1, H - 1], outline=PRETO, width=4)

    fnt_placa = _font(62)
    bb = draw.textbbox((0, 0), placa, font=fnt_placa)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    tx = (W - tw) // 2
    ty = (H - th) // 2 - 2
    draw.text((tx, ty), placa, font=fnt_placa, fill=PRETO)

    return img


def gerar_antigo_moto(placa: str) -> Image.Image:
    """
    200x140 px
    Faixa cinza/metálico superior 30px com texto "RJ CIDADE"
    Fundo branco, texto preto em duas linhas
    """
    W, H = 200, 140
    FAIXA_H = 30
    CINZA = (160, 160, 160)
    BRANCO = (255, 255, 255)
    PRETO = (0, 0, 0)

    img = Image.new("RGB", (W, H), BRANCO)
    draw = ImageDraw.Draw(img)

    draw.rectangle([0, 0, W - 1, H - 1], outline=PRETO, width=3)

    # Faixa cinza
    draw.rectangle([0, 0, W, FAIXA_H], fill=CINZA)

    fnt_cidade = _font(13)
    bb = draw.textbbox((0, 0), "RJ CIDADE", font=fnt_cidade)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    draw.text(((W - tw) // 2, (FAIXA_H - th) // 2 - 1), "RJ CIDADE", font=fnt_cidade, fill=PRETO)

    # Placa moto: 3 letras + 4 dígitos
    linha1 = placa[:3]
    linha2 = placa[3:]

    area_h = H - FAIXA_H
    fnt_placa = _font(36)

    bb1 = draw.textbbox((0, 0), linha1, font=fnt_placa)
    tw1, th1 = bb1[2] - bb1[0], bb1[3] - bb1[1]
    bb2 = draw.textbbox((0, 0), linha2, font=fnt_placa)
    tw2, th2 = bb2[2] - bb2[0], bb2[3] - bb2[1]

    total_text_h = th1 + 6 + th2
    start_y = FAIXA_H + (area_h - total_text_h) // 2

    draw.text(((W - tw1) // 2, start_y), linha1, font=fnt_placa, fill=PRETO)
    draw.text(((W - tw2) // 2, start_y + th1 + 6), linha2, font=fnt_placa, fill=PRETO)

    return img


GERADORES = {
    "mercosul_carro": gerar_mercosul_carro,
    "mercosul_moto": gerar_mercosul_moto,
    "antigo_carro": gerar_antigo_carro,
    "antigo_moto": gerar_antigo_moto,
}

# Mapeamento para campo "formato" do dataset
FORMATO_MAP = {
    "mercosul_carro": "mercosul",
    "mercosul_moto": "mercosul",
    "antigo_carro": "antigo",
    "antigo_moto": "antigo",
}


def gerar_todas() -> list[dict]:
    """Gera todas as imagens e retorna lista de entradas para o dataset."""
    import hashlib, time
    entradas = []
    for tipo, placas in PLACAS.items():
        gerador = GERADORES[tipo]
        for placa in placas:
            nome = f"synthetic_{placa}.jpg"
            caminho = FOTOS_DIR / nome
            img = gerador(placa)
            img.save(str(caminho), "JPEG", quality=95)
            print(f"  [{tipo}] {placa} -> {caminho}")

            # Gera id único curto
            h = hashlib.md5(f"syn_{placa}_{tipo}".encode()).hexdigest()[:8]

            entradas.append({
                "id": h,
                "arquivo": f"testes/fotos/{nome}",
                "placa_correta": placa,
                "formato": FORMATO_MAP[tipo],
                "tipo": "crop",
                "obs": f"sintético {tipo}",
            })
    return entradas


def atualizar_dataset(novas_entradas: list[dict]):
    """Adiciona as entradas ao dataset.json sem duplicar."""
    ds_path = ROOT / "testes" / "dataset.json"
    if ds_path.exists():
        ds = json.loads(ds_path.read_text(encoding="utf-8"))
    else:
        ds = {"version": 1, "fotos": []}

    ids_existentes = {f["id"] for f in ds.get("fotos", [])}
    arquivos_existentes = {f["arquivo"] for f in ds.get("fotos", [])}

    adicionados = 0
    for entrada in novas_entradas:
        if entrada["id"] not in ids_existentes and entrada["arquivo"] not in arquivos_existentes:
            ds.setdefault("fotos", []).append(entrada)
            adicionados += 1

    ds_path.write_text(json.dumps(ds, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nDataset atualizado: +{adicionados} entradas (total: {len(ds['fotos'])})")


if __name__ == "__main__":
    print("=== Gerando placas sintéticas ===")
    entradas = gerar_todas()
    atualizar_dataset(entradas)
    print("\nConcluído.")
