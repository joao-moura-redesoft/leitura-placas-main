# -*- coding: utf-8 -*-
"""Gera `app/web/static/favicon.ico` usando so a stdlib.

Pillow NAO esta em requirements.txt, e nao vale puxar uma dependencia de imagem para
desenhar 3 KB de icone. PNG e ICO sao os dois formatos mais simples de escrever a mao:
zlib + CRC32 para o PNG, um cabecalho de 22 bytes para o ICO.

O gerador fica no repo para o binario ser AUDITAVEL. Um .ico commitado sem gerador e um
blob que ninguem sabe reproduzir nem ajustar; com ele, mexer no desenho e rodar:

    python scripts/gerar_favicon.py

Desenho: quadrado arredondado no verde do `theme-color` do base.html (#16a34a) com uma
placa branca no meio e tres tarjas escuras fazendo os caracteres. A escolha e por
LEGIBILIDADE A 16px, onde texto de verdade viraria borrao: o que sobrevive nesse tamanho e
o contraste de duas formas grandes, e e por isso que a placa ocupa mais de metade da
largura. Antialias por supersampling 4x — o custo e irrelevante numa tela de 48px.
"""
import os
import struct
import sys
import zlib

VERDE = (0x16, 0xA3, 0x4A)
ESCURO = (0x0F, 0x17, 0x2A)
BRANCO = (0xFF, 0xFF, 0xFF)

TAMANHOS = (16, 32, 48)
SUPER = 4                      # subpixels por lado
SAIDA = os.path.join("app", "web", "static", "favicon.ico")


def _dentro_ret_arredondado(x, y, x0, y0, x1, y1, r):
    """`(x, y)` esta dentro do retangulo `[x0,x1]x[y0,y1]` com canto de raio `r`?"""
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    if r <= 0:
        return True
    # Só os quatro cantos precisam do teste de distancia; o resto do retangulo ja passou.
    cx = x0 + r if x < x0 + r else (x1 - r if x > x1 - r else x)
    cy = y0 + r if y < y0 + r else (y1 - r if y > y1 - r else y)
    if cx == x and cy == y:
        return True
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _desenhar(n):
    """Matriz `n x n` de (r, g, b, a), com antialias por media de `SUPER**2` amostras."""
    s = n * SUPER
    # Placa: 62% da largura, proporcao ~1,9:1 (a de uma placa real e 2:1 — arredondar para
    # baixo dá altura suficiente para as tarjas aparecerem a 16px).
    pl_w, pl_h = s * 0.62, s * 0.33
    pl_x0, pl_y0 = (s - pl_w) / 2, (s - pl_h) / 2
    pl_x1, pl_y1 = pl_x0 + pl_w, pl_y0 + pl_h
    # Tarjas: tres blocos dentro da placa, com vao igual entre eles.
    marg = pl_w * 0.11
    faixa_y0, faixa_y1 = pl_y0 + pl_h * 0.26, pl_y1 - pl_h * 0.26
    largura_util = pl_w - 2 * marg
    barra_w = largura_util * 0.22
    vao = (largura_util - 3 * barra_w) / 2
    barras = [(pl_x0 + marg + i * (barra_w + vao),
               pl_x0 + marg + i * (barra_w + vao) + barra_w) for i in range(3)]

    fundo_r = s * 0.22
    linhas = []
    for py in range(n):
        linha = []
        for px in range(n):
            acc_r = acc_g = acc_b = acc_a = 0
            for sy in range(SUPER):
                y = py * SUPER + sy + 0.5
                for sx in range(SUPER):
                    x = px * SUPER + sx + 0.5
                    if not _dentro_ret_arredondado(x, y, 0, 0, s - 1, s - 1, fundo_r):
                        continue                       # fora do icone: transparente
                    cor = VERDE
                    if _dentro_ret_arredondado(x, y, pl_x0, pl_y0, pl_x1, pl_y1,
                                               s * 0.045):
                        cor = BRANCO
                        if faixa_y0 <= y <= faixa_y1:
                            for bx0, bx1 in barras:
                                if bx0 <= x <= bx1:
                                    cor = ESCURO
                                    break
                    acc_r += cor[0]
                    acc_g += cor[1]
                    acc_b += cor[2]
                    acc_a += 255
            total = SUPER * SUPER
            if acc_a == 0:
                linha.append((0, 0, 0, 0))
                continue
            # Cor e a media das amostras COBERTAS (nao do total): sem isso a borda do
            # icone escurece contra o alfa, que e o halo cinza classico de antialias mal
            # feito. O alfa continua sendo sobre o total, que e o que ele mede.
            cobertas = acc_a // 255
            linha.append((acc_r // cobertas, acc_g // cobertas, acc_b // cobertas,
                          acc_a // total))
        linhas.append(linha)
    return linhas


def _chunk(tipo, dados):
    return (struct.pack(">I", len(dados)) + tipo + dados
            + struct.pack(">I", zlib.crc32(tipo + dados) & 0xFFFFFFFF))


def _png(pixels):
    n = len(pixels)
    cru = bytearray()
    for linha in pixels:
        cru.append(0)                                  # filtro 0 (None) por scanline
        for r, g, b, a in linha:
            cru += bytes((r, g, b, a))
    ihdr = struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0)  # 8 bits, RGBA, sem interlace
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(bytes(cru), 9)) + _chunk(b"IEND", b""))


def main():
    imagens = [(n, _png(_desenhar(n))) for n in TAMANHOS]
    # ICONDIR: reservado=0, tipo=1 (icone), quantidade.
    cab = struct.pack("<HHH", 0, 1, len(imagens))
    offset = len(cab) + 16 * len(imagens)
    entradas, corpo = b"", b""
    for n, png in imagens:
        # planos=1, bits=32; contagem de cores 0 = "nao usa paleta".
        entradas += struct.pack("<BBBBHHII", n, n, 0, 0, 1, 32, len(png), offset)
        corpo += png
        offset += len(png)
    os.makedirs(os.path.dirname(SAIDA), exist_ok=True)
    with open(SAIDA, "wb") as fh:
        fh.write(cab + entradas + corpo)
    print(f"{SAIDA}: {len(cab + entradas + corpo)} bytes, "
          f"{', '.join(f'{n}x{n}' for n in TAMANHOS)}")


if __name__ == "__main__":
    sys.exit(main())
