"""Validação e normalização de placas brasileiras (Mercosul + antigo).

Padrões:
  - Antigo:    AAA0000  (3 letras + 4 dígitos)
  - Mercosul:  AAA0A00  (3 letras + 1 dígito + 1 letra + 2 dígitos)

Aplica correções posicionais de OCR (O↔0, I↔1, B↔8, S↔5, Z↔2, G↔6) somente
nas posições onde o padrão exige letra ou dígito.

Quando o OCR retorna texto mais longo que 7 chars (ex: leu "BRASIL" do
cabeçalho Mercosul junto com a placa), usa janela deslizante de 7 chars
para encontrar uma sequência válida dentro do texto bruto.
"""
from __future__ import annotations
import re

LETRA_PARA_DIGITO = {"O": "0", "I": "1", "B": "8", "S": "5", "Z": "2", "G": "6", "Q": "0", "D": "0"}
DIGITO_PARA_LETRA = {"0": "O", "1": "I", "7": "T", "8": "B", "5": "S", "2": "Z", "6": "G", "3": "J"}

# Dígitos na posição 4 (índice 4) que são visualmente muito semelhantes a letras
# na fonte FE-Schrift / DIN 1451 usada nas placas Mercosul BR.
# Quando o OCR retorna esses dígitos nessa posição tentamos a correção Mercosul.
# Nota: antigo direto é testado ANTES desta etapa, portanto antigos genuínos
# (ex: AAA0001, LSN4149) não são afetados.
# Mapeamentos: 0→O, 1→I, 8→B (clássicos), 3→J (J muito parecido com 3 nessa fonte),
#              5→S, 2→Z, 6→G (cobertos indiretamente pelo _corrigir geral).
_AMBIGUOS_POS4 = frozenset("01235678")

RE_ANTIGO = re.compile(r"^[A-Z]{3}[0-9]{4}$")
RE_MERCOSUL = re.compile(r"^[A-Z]{3}[0-9][A-Z][0-9]{2}$")

# Posições esperadas: L = letra, D = dígito
POSICOES_ANTIGO = list("LLLDDDD")
POSICOES_MERCOSUL = list("LLLDLDD")


def _corrigir(texto: str, posicoes: list[str]) -> str:
    out = []
    for ch, alvo in zip(texto, posicoes):
        if alvo == "L" and ch.isdigit():
            out.append(DIGITO_PARA_LETRA.get(ch, ch))
        elif alvo == "D" and ch.isalpha():
            out.append(LETRA_PARA_DIGITO.get(ch, ch))
        else:
            out.append(ch)
    return "".join(out)


def _validar_7(trecho: str, formato_hint: str = "") -> tuple[str, str] | None:
    """Tenta validar exatamente 7 chars — direto e com correções posicionais.

    formato_hint:
      'mercosul_moto' : moto Mercosul (layout 2 linhas, aspecto do crop confirma) — a
                        correção Mercosul tem prioridade sobre um match antigo direto,
                        pois o layout já é um sinal forte e confiável.
      'mercosul'      : carro — hint vem só da cor do header, menos confiável; um match
                        antigo direto e limpo NUNCA é corrompido por este hint.
    """
    if RE_MERCOSUL.match(trecho):
        return trecho, "mercosul"

    # Moto Mercosul (layout 2 linhas): a letra da posição 5 é frequentemente confundida
    # pelo OCR com um dígito visualmente parecido (I→1, O→0, Q→0, T→7 na fonte da placa).
    # Aqui confiamos no hint ANTES do match direto de antigo — senão strings como
    # "FBI0123" (OCR de "FBI0I23") nunca seriam corrigidas. A visão de moto é confiável
    # (aspecto ≤2 do crop já confirma o layout, não depende só da cor do header).
    if formato_hint == "mercosul_moto":
        c = _corrigir(trecho, POSICOES_MERCOSUL)
        if RE_MERCOSUL.match(c):
            return c, "mercosul"

    # Antigo direto tem prioridade sobre correção Mercosul de CARRO (single-line) — evita
    # que um texto já CORRETAMENTE lido como antigo (ex: CDV2112) seja corrompido por um
    # falso-positivo do detector de header por cor (ex: cartão de teste com borda colorida
    # confundida com faixa Mercosul). Diferente da moto, aqui o hint vem só da cor — menos
    # confiável — então um match direto e limpo tem prioridade sobre ele.
    if RE_ANTIGO.match(trecho):
        return trecho, "antigo"

    # Com hint Mercosul (carro, ou moto que não bateu acima): tenta correção antes das
    # correções genéricas por dígito ambíguo abaixo.
    if formato_hint in ("mercosul", "mercosul_moto"):
        c = _corrigir(trecho, POSICOES_MERCOSUL)
        if RE_MERCOSUL.match(c):
            return c, "mercosul"
    # Só aqui tenta correção Mercosul para dígitos ambíguos na pos-4 (8≈B, 0≈O, 1≈I).
    # Neste ponto sabemos que o texto não bate diretamente nem como Mercosul nem antigo.
    if trecho[4] in _AMBIGUOS_POS4:
        c = _corrigir(trecho, POSICOES_MERCOSUL)
        if RE_MERCOSUL.match(c):
            return c, "mercosul"
    c = _corrigir(trecho, POSICOES_MERCOSUL)
    if RE_MERCOSUL.match(c):
        return c, "mercosul"
    c = _corrigir(trecho, POSICOES_ANTIGO)
    if RE_ANTIGO.match(c):
        return c, "antigo"
    return None


def validar(texto: str, formato_hint: str = "") -> tuple[str, str] | None:
    """Retorna (placa_normalizada, padrao) ou None se inválida.

    formato_hint: 'mercosul' para priorizar correção Mercosul antes de aceitar
    antigo direto. Útil quando o caller detectou header Mercosul na imagem.

    Quando o OCR devolve mais de 7 chars (ex: leu o cabeçalho "BRASIL"
    junto com a placa), aplica janela deslizante de 7 chars para encontrar
    a sequência válida dentro do texto bruto.
    """
    if not texto:
        return None
    bruto = re.sub(r"[^A-Z0-9]", "", texto.upper())
    n = len(bruto)
    if n < 7:
        return None

    if n == 7:
        return _validar_7(bruto, formato_hint)

    # Texto maior que 7 — OCR leu artefatos (cabeçalho, QR, marcadores).
    # Varre janelas de 7 chars; prefere match sem correção (mais confiável).
    # Primeira passagem: matches diretos — antigo tem prioridade sobre mercosul corrigido
    for i in range(n - 6):
        t = bruto[i: i + 7]
        if RE_MERCOSUL.match(t):
            return t, "mercosul"
        if RE_ANTIGO.match(t):
            return t, "antigo"

    # Com hint Mercosul: tenta correção Mercosul antes de aceitar antigo
    if formato_hint in ("mercosul", "mercosul_moto"):
        for i in range(n - 6):
            t = bruto[i: i + 7]
            c = _corrigir(t, POSICOES_MERCOSUL)
            if RE_MERCOSUL.match(c):
                return c, "mercosul"

    # Segunda passagem: aceita mercosul corrigido para pos-4 ambígua
    for i in range(n - 6):
        t = bruto[i: i + 7]
        if t[4] in _AMBIGUOS_POS4:
            c = _corrigir(t, POSICOES_MERCOSUL)
            if RE_MERCOSUL.match(c):
                return c, "mercosul"

    # Terceira passagem: aceita com correções posicionais completas
    for i in range(n - 6):
        t = bruto[i: i + 7]
        c = _corrigir(t, POSICOES_MERCOSUL)
        if RE_MERCOSUL.match(c):
            return c, "mercosul"
        c = _corrigir(t, POSICOES_ANTIGO)
        if RE_ANTIGO.match(c):
            return c, "antigo"

    return None


def parecidas(a: str, b: str, max_diff: int = 2) -> bool:
    """True se `a` e `b` têm o mesmo tamanho e diferem em até `max_diff` posições.

    Usado para tratar duas leituras próximas no tempo (mesmo bico/câmera) como o
    MESMO veículo apesar de ruído de OCR: confusões clássicas como 0/O/D/Q ou I/1/J
    trocam 1-2 caracteres sem trocar o carro. Evita que esse ruído vire duas linhas
    diferentes no histórico.
    """
    if len(a) != len(b):
        return False
    return sum(1 for x, y in zip(a, b) if x != y) <= max_diff


def formatar(placa: str, padrao: str) -> str:
    """Aplica hífen para exibição (ex: ABC-1234 / ABC1D23)."""
    if padrao == "antigo" and len(placa) == 7:
        return f"{placa[:3]}-{placa[3:]}"
    return placa
