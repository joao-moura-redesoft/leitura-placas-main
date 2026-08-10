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


@pytest.mark.parametrize("texto, esperado", [
    ("1234567", "IZJ4S67"),      # só dígitos
    ("0000000", "OOO0O00"),
])
def test_texto_sem_placa_nenhuma_ainda_pode_virar_placa(texto, esperado):
    """Comportamento DELIBERADO, fixado aqui para não mudar sem querer.

    A correção posicional de última instância troca dígito↔letra em qualquer posição
    onde o padrão peça o outro tipo. Quando todas as trocas necessárias existem no mapa
    de confusões (0↔O, 1↔I, 2↔Z, 3↔J, 5↔S, 6↔G, 8↔B), texto que não é placa nenhuma sai
    daqui como placa "válida" — uma sequência de dígitos lida de um preço ou de um
    telefone, por exemplo. É o mesmo mecanismo que recupera placa borrada de verdade.

    Quem barra esse falso positivo é o resto da pilha, não esta função: detecção em 2
    estágios (a placa tem que estar dentro de um veículo), consenso entre frames e o
    limiar de confiança. Mexer aqui altera a acurácia medida contra o UFPR-ALPR.
    """
    placa, _padrao = validar(texto)
    assert placa == esperado


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
