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

# Máximo de trocas dígito↔letra aceitas para encaixar um texto num padrão de placa.
#
# Sem teto, a correção posicional encaixa QUALQUER coisa: uma sequência de 7 dígitos vira
# placa Mercosul em 51,1% dos casos (medido em 100k strings aleatórias), porque o padrão
# LLLDLDD só precisa que as 4 posições de letra tenham mapeamento no dicionário de
# confusões. O teto separa os dois usos do mesmo mecanismo: recuperar uma placa REAL cujo
# OCR trocou 1-2 caracteres (o que ele foi feito para fazer) e fabricar uma placa a partir
# de texto que nunca foi placa (o que ele fazia de graça junto).
#
# 2 é onde a separação é limpa nas correções legítimas conhecidas: 'FBI0123'→'FBI0I23'
# custa 1, 'ABCO234'→'ABC0Z34' custa 2, e os falsos positivos de dígito puro custam 4
# (3 posições de letra + a posição 4) — nenhum encosta no teto pelo lado errado.
MAX_CORRECOES = 2


def _corrigir(texto: str, posicoes: list[str]) -> tuple[str, int]:
    """Aplica as trocas posicionais e devolve (texto_corrigido, nº de trocas aplicadas).

    Só conta como troca o caractere que REALMENTE mudou: um dígito sem mapeamento numa
    posição de letra (ex.: '9', '4') sai intacto e não consome orçamento — o encaixe
    falha depois, na regex, como já falhava antes.
    """
    out = []
    n = 0
    for ch, alvo in zip(texto, posicoes):
        if alvo == "L" and ch.isdigit():
            novo = DIGITO_PARA_LETRA.get(ch, ch)
        elif alvo == "D" and ch.isalpha():
            novo = LETRA_PARA_DIGITO.get(ch, ch)
        else:
            novo = ch
        if novo != ch:
            n += 1
        out.append(novo)
    return "".join(out), n


def _encaixar(trecho: str, posicoes: list[str], regex: re.Pattern, padrao: str,
              max_correcoes: int) -> tuple[str, str] | None:
    """Tenta encaixar `trecho` no padrão, respeitando o orçamento de correções."""
    c, n = _corrigir(trecho, posicoes)
    if n <= max_correcoes and regex.match(c):
        return c, padrao
    return None


def _validar_7(trecho: str, formato_hint: str = "",
               max_correcoes: int = MAX_CORRECOES) -> tuple[str, str] | None:
    """Tenta validar exatamente 7 chars — direto e com correções posicionais.

    formato_hint:
      'mercosul'      : carro — hint vem só da cor do header, menos confiável; um match
                        antigo direto e limpo NUNCA é corrompido por este hint.

    O hint 'mercosul_moto' foi REMOVIDO em 25/08/2026. Ele era o único com poder de
    sobrepor um match antigo direto, e usava esse poder às cegas: `validar` não vê a
    confiança POR CARACTERE, então reescrevia posição que o modelo tinha lido com
    certeza. Na moto antiga metálica OSL2659, o detector de faixa deu falso positivo, o
    hint entrou, e 'OSL2655' — cuja posição 4 veio com 0,99 de confiança — virou
    'OSL2G55': um erro de 1 caractere passou a 2 e o padrão inverteu. Na Mercosul de moto
    RLX2A77 o mesmo detector deu falso NEGATIVO com a faixa azul visível, ou seja, a
    única fonte do hint errou nos dois sentidos nas duas motos medidas.

    ATENÇÃO ao mexer aqui: com 'mercosul_moto' fora, o hint que sobrou é INERTE. Medido em
    200.000 strings de 7 caracteres aleatórias, `validar(t, '')` e `validar(t, 'mercosul')`
    deram resultado idêntico em 100% dos casos — zero diferenças. É consequência da ordem
    das tentativas: quando o hint fraco é consultado, as passagens seguintes já tentariam a
    mesma correção Mercosul de qualquer jeito. O parâmetro continua na assinatura porque
    vários chamadores o passam, mas ninguém deve contar com ele para corrigir nada, e
    ninguém deve "consertar" um bug passando um hint. Remover o parâmetro de vez é limpeza
    para uma próxima passada, com os chamadores no mesmo commit.

    Quem corrige erro de 1-2 caracteres hoje é a fusão por posição entre várias leituras
    (`visao.consenso.consenso_caractere`), que pondera por confiança em vez de adivinhar
    o layout: 'FBI0123' vindo de 'FBI0I23' se resolve com o voto de outra leitura, sem
    precisar de um palpite sobre a diagramação da placa.
    """
    if RE_MERCOSUL.match(trecho):
        return trecho, "mercosul"

    # Antigo direto tem prioridade sobre correção Mercosul de CARRO (single-line) — evita
    # que um texto já CORRETAMENTE lido como antigo (ex: CDV2112) seja corrompido por um
    # falso-positivo do detector de header por cor (ex: cartão de teste com borda colorida
    # confundida com faixa Mercosul). Diferente da moto, aqui o hint vem só da cor — menos
    # confiável — então um match direto e limpo tem prioridade sobre ele.
    if RE_ANTIGO.match(trecho):
        return trecho, "antigo"

    # Com hint Mercosul (cor do header): tenta correção antes das correções genéricas por
    # dígito ambíguo abaixo.
    if formato_hint == "mercosul":
        r = _encaixar(trecho, POSICOES_MERCOSUL, RE_MERCOSUL, "mercosul", max_correcoes)
        if r:
            return r
    # Só aqui tenta correção Mercosul para dígitos ambíguos na pos-4 (8≈B, 0≈O, 1≈I).
    # Neste ponto sabemos que o texto não bate diretamente nem como Mercosul nem antigo.
    if trecho[4] in _AMBIGUOS_POS4:
        r = _encaixar(trecho, POSICOES_MERCOSUL, RE_MERCOSUL, "mercosul", max_correcoes)
        if r:
            return r
    r = _encaixar(trecho, POSICOES_MERCOSUL, RE_MERCOSUL, "mercosul", max_correcoes)
    if r:
        return r
    return _encaixar(trecho, POSICOES_ANTIGO, RE_ANTIGO, "antigo", max_correcoes)


def validar(texto: str, formato_hint: str = "",
            max_correcoes: int = MAX_CORRECOES) -> tuple[str, str] | None:
    """Retorna (placa_normalizada, padrao) ou None se inválida.

    formato_hint: 'mercosul' para priorizar correção Mercosul antes de aceitar
    antigo direto. Útil quando o caller detectou header Mercosul na imagem.

    max_correcoes: orçamento de trocas dígito↔letra por encaixe (ver MAX_CORRECOES).
    Passar um valor alto (>=4) reproduz o comportamento anterior ao teto.

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
        return _validar_7(bruto, formato_hint, max_correcoes)

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
    if formato_hint == "mercosul":
        for i in range(n - 6):
            r = _encaixar(bruto[i: i + 7], POSICOES_MERCOSUL, RE_MERCOSUL, "mercosul", max_correcoes)
            if r:
                return r

    # Segunda passagem: aceita mercosul corrigido para pos-4 ambígua
    for i in range(n - 6):
        t = bruto[i: i + 7]
        if t[4] in _AMBIGUOS_POS4:
            r = _encaixar(t, POSICOES_MERCOSUL, RE_MERCOSUL, "mercosul", max_correcoes)
            if r:
                return r

    # Terceira passagem: aceita com correções posicionais completas
    for i in range(n - 6):
        t = bruto[i: i + 7]
        r = (_encaixar(t, POSICOES_MERCOSUL, RE_MERCOSUL, "mercosul", max_correcoes)
             or _encaixar(t, POSICOES_ANTIGO, RE_ANTIGO, "antigo", max_correcoes))
        if r:
            return r

    return None


def alternativas_de_linha(texto: str) -> list[str]:
    """Leituras de 7 chars alternativas quando o texto e "letras seguidas de digitos".

    `validar` sozinha nao resolve este caso porque ela devolve UMA resposta e escolhe a
    primeira janela que casa, da esquerda para a direita. Em `OSL12659` a primeira janela e
    `OSL1265`: mantem o `1` esporio e descarta o `9`, que era digito de verdade. A placa
    correta era `OSL2659`, e nenhuma janela de 7 caracteres CONTIGUOS a produz - o `1` esta
    no MEIO do texto, entre as letras e os digitos.

    E o modo de falha classico de placa de duas linhas: o OCR concatena a linha de cima com
    a de baixo e um artefato da borda (parafuso, hifen, moldura) entra como caractere no
    comeco de uma das linhas. Por isso a extracao aqui e por ESTRUTURA (3 letras + 4
    digitos), tomando as duas pontas de cada bloco.

    So a estrutura, e NAO janela deslizante: `validar` ja varre janelas, e com uma ordem de
    prioridade que importa (casamento direto antes de correcao posicional). Substituir essa
    ordem por "todas as janelas que validam, com peso igual" foi um bug medido: em
    `BRASILABC1D23` a janela `ILABC1D` valida como `ILA8C10` gastando 2 correcoes, empatava
    em peso com a placa de verdade e chegava a ganhar a eleicao. Quem chama deve usar o
    resultado de `validar` como candidato PRINCIPAL e estas alternativas como adicionais.

    Devolve candidatos, no plural, de proposito: qual deles e a placa nao se decide olhando
    UMA leitura. Quem chama joga todos no pool e deixa a fusao por posicao votar contra as
    leituras dos outros engines - a unica informacao capaz de desempatar sem chute sobre de
    que lado o artefato entrou.
    """
    bruto = re.sub(r"[^A-Z0-9]", "", (texto or "").upper())
    if len(bruto) < 8:
        return []
    m = re.match(r"^([A-Z]+)([0-9]+)$", bruto)
    if not m:
        return []
    letras, digitos = m.group(1), m.group(2)
    if len(letras) < 3 or len(digitos) < 4:
        return []
    vistos: list[str] = []
    for ini_l in dict.fromkeys((0, len(letras) - 3)):
        for ini_d in dict.fromkeys((0, len(digitos) - 4)):
            c = letras[ini_l:ini_l + 3] + digitos[ini_d:ini_d + 4]
            if len(c) == 7 and c not in vistos:
                vistos.append(c)
    return vistos


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
