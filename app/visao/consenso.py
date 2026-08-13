"""Regra única que separa uma leitura sólida da candidata menos ruim.

Módulo próprio porque os DOIS caminhos de leitura precisam dela e nenhum pode importar
o outro: `leitura.py` (GET reativo) já importa `pipeline.py` (`_expandir_bbox`), então o
inverso fecharia um ciclo de import. A alternativa era duplicar a regra nos dois lados —
e uma regra de consenso duplicada é uma regra que diverge na primeira vez que alguém
ajusta só um dos lados. Aqui `leitura_acordo_minimo` continua significando a mesma coisa
esteja a placa vindo do bico ou do monitoramento contínuo.
"""
from __future__ import annotations


def confirmada(acordo: float, n_votos: int, acordo_min: float, n_min: int) -> bool:
    """Decide se a placa eleita é uma leitura sólida ou só a candidata menos ruim.

    Vale como confirmada quando o acordo bate o mínimo E pelo menos 2 leituras
    INDEPENDENTES votaram nela. As duas condições são necessárias, e a segunda não é
    redundante: `acordo` é uma FRAÇÃO, e uma fração sobre uma amostra de tamanho 1 vale
    1.0 sem que nada tenha concordado com nada.

    Foi exatamente assim que uma chamada numa pista VAZIA voltou confirmada: o detector
    deu falso positivo sobre o asfalto (recorte de 330x296px, proporção 1,1:1 — placa
    nenhuma), um engine "leu" 7 caracteres, o outro não leu nada, e acordo=1.0 com
    1 voto virou `status: ok` na taxa de sucesso do painel.

    O que conta como "uma leitura" muda com o caminho, e é por isso que quem chama passa
    os números já contados em vez de passar o pool cru:

      - leitura reativa (`leitura.py`): um voto é uma FOTO do loop reject-retry. O pool de
        `_eleger_placa` mistura a placa de cada candidato com cada engine dele, então um
        único frame entra várias vezes — a contagem que importa é a de fotos distintas.
      - contínuo, modo tracker (`pipeline.py`): um voto é uma passada de OCR no mesmo
        veículo rastreado, em frames diferentes.
      - contínuo, modo clássico: um voto é um frame consecutivo com a mesma placa.

    `min(2, n_min)` respeita quem configurou 1 voto (`snapshots_votacao`,
    `frames_consenso` ou `tracker_votos_emitir`): nesse modo o operador abriu mão da
    votação entre leituras, e exigir 2 votos deixaria TODA leitura não-confirmada — o
    oposto do ajuste que ele pediu. Com os padrões (3, 3 e 2), exige os 2 votos de verdade.
    """
    return acordo >= acordo_min and n_votos >= min(2, n_min)
