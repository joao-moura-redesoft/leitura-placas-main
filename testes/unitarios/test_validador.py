"""Validação/normalização de placa — o que decide se uma leitura de OCR vira placa."""
from __future__ import annotations

import pytest

from app.visao.validador import formatar, parecidas, validar


@pytest.mark.parametrize("texto, esperado", [
    ("ABC1234", ("ABC1234", "antigo")),
    ("ABC1D23", ("ABC1D23", "mercosul")),
    ("abc1d23", ("ABC1D23", "mercosul")),      # minúscula normalizada
    ("ABC-1234", ("ABC1234", "antigo")),       # separador descartado
    ("ABC 1D23", ("ABC1D23", "mercosul")),
])
def test_aceita_placa_valida(texto, esperado):
    assert validar(texto) == esperado


@pytest.mark.parametrize("texto", ["", "ABC12", "AB1234", "ABCDEF", "ABCDEFG", None])
def test_recusa_texto_que_nao_e_placa(texto):
    assert validar(texto) is None


@pytest.mark.parametrize("texto", ["1234567", "0000000", "5551234", "8888888"])
def test_sequencia_de_digitos_nao_vira_mais_placa(texto):
    """Reverte um comportamento que ERA deliberado, com medição — ver MAX_CORRECOES.

    Até 2026-08-12 estes textos saíam daqui como placa Mercosul ('1234567'→'IZJ4S67'),
    e a versão anterior deste teste fixava isso de propósito, com o argumento de que quem
    barraria o falso positivo seria o resto da pilha: 2 estágios, consenso entre frames e
    limiar de confiança. Medindo, o argumento não se sustenta:

    - Escala: 51,1% de 100k sequências aleatórias de 7 dígitos viravam placa "válida"
      (7 alfanuméricos: 10,3%; 8-14 chars, via janela deslizante: 36,4%).
    - 2 estágios não cobre: `veiculo_obrigatorio=nao` é o padrão (cai para o frame
      inteiro), e o texto que mais gera esse falso positivo — telefone de frota pintado
      na porta, numeração de tanque — está DENTRO do recorte do veículo.
    - Consenso e limiar não cobrem: os dois filtram ruído ALEATÓRIO entre frames. Um
      texto fixo pintado no veículo é lido igual em todo frame, então tira acordo alto e
      sai `confirmada=1` — o consenso reforça o falso positivo em vez de barrá-lo.

    O teto de MAX_CORRECOES=2 separa os dois usos do mesmo mecanismo. Os falsos positivos
    de dígito puro custam 4 correções no padrão Mercosul e 3 no antigo; as correções
    legítimas conhecidas custam 1 e 2 (ver os dois testes abaixo, que continuam valendo).
    Por isso o teto é 2 e não 3: com 3, o encaixe no padrão antigo (LLLDDDD, só 3 posições
    de letra) deixa passar exatamente os mesmos 51,1%.

    Custo medido no `dataset.json` (41 itens, instrumentando o custo de cada encaixe
    aceito numa passada com o teto aberto): 1 leitura perdida, a sintética `EQG0Q00`, que
    precisava de 3 correções. As 5 fotos REAIS não foram afetadas (4 casam direto, 1 custa
    1 correção), e das leituras mantidas 10 custam 1 correção e 1 custa 2 — ou seja, a
    correção posicional continua carregando peso, e o teto preserva o uso legítimo dela.

    ATENÇÃO ao que essa medição NÃO cobre: o dataset é quase todo sintético e não inclui
    o UFPR-ALPR, que era a preocupação declarada da versão anterior deste teste. Se for
    calibrar o teto contra placa real borrada, é lá que a conta muda — o parâmetro
    `max_correcoes` existe para medir os dois lados sem editar código.
    """
    assert validar(texto) is None


def test_teto_de_correcoes_e_parametrizavel():
    """O teto é um parâmetro para o caminho de avaliação poder medir os dois lados
    (`max_correcoes=99` reproduz o comportamento anterior, sem teto)."""
    assert validar("1234567", max_correcoes=99) == ("IZJ4S67", "mercosul")
    assert validar("1234567", max_correcoes=2) is None


def test_correcao_mercosul_tem_prioridade_sobre_antigo_corrigido():
    """'ABCO234' poderia virar 'ABC0234' (antigo) ou 'ABC0Z34' (Mercosul). Com o dígito
    ambíguo na posição 4, o Mercosul é tentado primeiro — é o formato em circulação."""
    assert validar("ABCO234") == ("ABC0Z34", "mercosul")
    # Já um antigo que casa direto não é tocado.
    assert validar("ABC0234") == ("ABC0234", "antigo")


def test_placa_antiga_legitima_nao_vira_mercosul():
    """Regressão: o hint de Mercosul vinha da cor do cabeçalho e podia ser falso
    positivo. Uma placa antiga que já casa direto não pode ser 'corrigida'."""
    assert validar("CDV2112", formato_hint="mercosul") == ("CDV2112", "antigo")


def test_hint_de_moto_corrige_digito_na_posicao_de_letra():
    """Na moto o layout de 2 linhas confirma o formato, então o hint tem prioridade:
    'FBI0123' lido de uma Mercosul precisa virar 'FBI0I23'."""
    assert validar("FBI0123", formato_hint="mercosul_moto") == ("FBI0I23", "mercosul")


def test_extrai_placa_de_texto_com_lixo_em_volta():
    """OCR costuma trazer o 'BRASIL' do cabeçalho junto — a janela deslizante tem
    que achar a placa dentro do texto."""
    assert validar("BRASILABC1D23") == ("ABC1D23", "mercosul")


@pytest.mark.parametrize("a, b, max_diff, esperado", [
    ("ABC1D23", "ABC1D23", 2, True),
    ("ABC1D23", "ABC1023", 2, True),      # 1 caractere de ruído
    ("ABC1D23", "ABC1O24", 2, True),      # 2 caracteres
    ("ABC1D23", "XYZ9K88", 2, False),
    ("ABC1D23", "ABC1D2", 2, False),      # tamanhos diferentes nunca são parecidas
])
def test_parecidas(a, b, max_diff, esperado):
    assert parecidas(a, b, max_diff=max_diff) is esperado


def test_formatar_poe_hifen_so_no_padrao_antigo():
    assert formatar("ABC1234", "antigo") == "ABC-1234"
    assert formatar("ABC1D23", "mercosul") == "ABC1D23"
