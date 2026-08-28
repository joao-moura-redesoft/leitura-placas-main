"""Validação/normalização de placa — o que decide se uma leitura de OCR vira placa."""
from __future__ import annotations

import pytest

from app.visao.validador import alternativas_de_linha, formatar, parecidas, validar


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


def test_hint_de_moto_nao_reescreve_mais_placa_que_casa_como_antiga():
    """O hint 'mercosul_moto' foi REMOVIDO em 25/08/2026 — este teste guarda a remoção.

    Ele era o único hint com poder de sobrepor um match antigo direto, e usava esse poder às
    cegas: `validar` não vê confiança POR CARACTERE. Na moto antiga metálica OSL2659 (bico
    3 do ALTIPLANO, 24/08/2026) o detector de faixa deu falso positivo, o hint entrou, e
    'OSL2655' virou 'OSL2G55' — reescrevendo a posição 4, que o modelo havia lido com 0,99
    de confiança. Um erro de 1 caractere passou a 2 e o padrão inverteu.

    'FBI0123' — o caso que este teste defendia — casa como antiga e agora fica como antiga.
    Se ela era de fato uma Mercosul mal lida, quem conserta é a fusão por posição entre
    várias leituras (`visao.consenso.consenso_caractere`), que pondera por confiança em vez
    de adivinhar a diagramação a partir da cor de uma faixa.
    """
    assert validar("FBI0123", formato_hint="mercosul_moto") == ("FBI0123", "antigo")
    assert validar("FBI0123") == ("FBI0123", "antigo")


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


class TestAlternativasDeLinha:
    """Extração por ESTRUTURA, para placa de duas linhas que o OCR concatenou.

    `validar('OSL12659')` devolve `OSL1265`: a janela deslizante para na primeira que casa,
    da esquerda para a direita, e essa mantém o `1` espúrio e descarta o `9`, que era dígito
    de verdade. A placa era `OSL2659` — e nenhuma janela de 7 caracteres CONTÍGUOS a produz,
    porque o caractere sobrando está no meio do texto, entre as letras e os dígitos.

    Caso real: bico 3 do ALTIPLANO, 24/08/2026. A EasyOCR devolveu as caixas `'OSL'` e
    `'12659'`, o `'2659'` com 0,99 de confiança.
    """

    def test_gera_as_duas_leituras_plausiveis_de_letras_mais_digitos(self):
        alts = alternativas_de_linha("OSL12659")
        assert "OSL2659" in alts, "a placa correta tem de estar entre as alternativas"
        assert "OSL1265" in alts, "a leitura da janela antiga continua sendo alternativa"

    def test_nao_escolhe_por_conta_propria(self):
        """A função não decide — quem decide é o voto contra os outros engines.

        Fixado porque a tentação óbvia é "prefira sempre os 4 últimos dígitos". Isso seria
        um palpite sobre de que lado o artefato de borda entrou, calibrado em UM caso.
        """
        assert len(alternativas_de_linha("OSL12659")) > 1

    def test_nao_faz_janela_deslizante(self):
        """Só ESTRUTURA letras+dígitos. Janela é trabalho de `validar`, que tem a ordem de
        prioridade certa (casamento direto antes de correção posicional).

        Regressão medida: com janela deslizante aqui, `BRASILABC1D23` gerava `ILABC1D`, que
        valida como `ILA8C10` gastando 2 correções, empatava em peso com a placa real e
        chegava a GANHAR a eleição no `_fundir`.
        """
        assert alternativas_de_linha("BRASILABC1D23") == []

    def test_texto_de_7_nao_tem_alternativa(self):
        assert alternativas_de_linha("ABC1D23") == []
        assert alternativas_de_linha("ABC1234") == []

    def test_curto_demais_nao_gera_nada(self):
        """`OS2659` (6 chars) não vira placa por invenção de caractere."""
        assert alternativas_de_linha("OS2659") == []
        assert alternativas_de_linha("") == []

    def test_sem_bloco_suficiente_nao_gera_nada(self):
        """Precisa de ao menos 3 letras E 4 dígitos para haver o que recortar."""
        assert alternativas_de_linha("AB123456") == []
        assert alternativas_de_linha("ABCDE123") == []

    def test_sem_duplicata(self):
        alts = alternativas_de_linha("AAA11111")
        assert len(alts) == len(set(alts))
