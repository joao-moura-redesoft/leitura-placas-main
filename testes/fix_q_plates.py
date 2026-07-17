"""Regenera placas com Q em posição 4 como PNG lossless (4x supersampling + LANCZOS)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path
import json
from PIL import Image, ImageDraw, ImageFont

FOTOS_DIR = Path("testes/fotos")
FONT_PATH = "C:/Windows/Fonts/arialbd.ttf"
DS_PATH = Path("testes/dataset.json")


def _font(size): return ImageFont.truetype(FONT_PATH, size)


def gerar_mercosul_moto_hq(placa: str) -> Image.Image:
    """200x140 renderizado em 4x (800x560) e reduzido com LANCZOS."""
    W, H = 800, 560
    FAIXA_H = 120
    AZUL = (0, 51, 153)
    BRANCO = (255, 255, 255)
    PRETO = (0, 0, 0)

    img = Image.new("RGB", (W, H), BRANCO)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, W - 1, H - 1], outline=PRETO, width=12)
    draw.rectangle([0, 0, W, FAIXA_H], fill=AZUL)

    fnt_brasil = _font(52)
    bb = draw.textbbox((0, 0), "BRASIL", font=fnt_brasil)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    draw.text(((W - tw) // 2, (FAIXA_H - th) // 2 - 4), "BRASIL", font=fnt_brasil, fill=BRANCO)

    linha1, linha2 = placa[:3], placa[3:]
    area_h = H - FAIXA_H
    fnt = _font(144)

    bb1 = draw.textbbox((0, 0), linha1, font=fnt)
    tw1, th1 = bb1[2] - bb1[0], bb1[3] - bb1[1]
    bb2 = draw.textbbox((0, 0), linha2, font=fnt)
    tw2, th2 = bb2[2] - bb2[0], bb2[3] - bb2[1]

    total = th1 + 24 + th2
    sy = FAIXA_H + (area_h - total) // 2
    draw.text(((W - tw1) // 2, sy), linha1, font=fnt, fill=PRETO)
    draw.text(((W - tw2) // 2, sy + th1 + 24), linha2, font=fnt, fill=PRETO)

    return img.resize((200, 140), Image.LANCZOS)


CASOS = [
    ("synthetic_NOP5Q67", "NOP5Q67", "testes/fotos/synthetic_NOP5Q67.jpg"),
    ("synthetic_moto_mercosul_KQR5Q89", "KQR5Q89", "testes/fotos/synthetic_moto_mercosul_KQR5Q89.jpg"),
]

ds = json.loads(DS_PATH.read_text(encoding="utf-8"))

for key, placa, arquivo_antigo in CASOS:
    nome_png = key + ".png"
    caminho_png = FOTOS_DIR / nome_png
    arquivo_novo = f"testes/fotos/{nome_png}"

    img = gerar_mercosul_moto_hq(placa)
    img.save(str(caminho_png), "PNG")
    print(f"  Saved: {caminho_png}")

    # Atualiza dataset.json: troca extensão .jpg → .png
    for foto in ds["fotos"]:
        if foto["arquivo"] == arquivo_antigo:
            foto["arquivo"] = arquivo_novo
            print(f"  Dataset: {arquivo_antigo} -> {arquivo_novo}")
            break

DS_PATH.write_text(json.dumps(ds, indent=2, ensure_ascii=False), encoding="utf-8")
print("Dataset atualizado.")
