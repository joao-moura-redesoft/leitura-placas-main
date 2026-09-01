"""Como N leituras ruidosas viram uma placa só, e o que separa leitura sólida de chute.

Módulo próprio porque os DOIS caminhos de leitura precisam disto e nenhum pode importar
o outro: `leitura.py` (GET reativo) já importa `pipeline.py` (`_expandir_bbox`), então o
inverso fecharia um ciclo de import. A alternativa era duplicar a regra nos dois lados —
e uma regra de consenso duplicada é uma regra que diverge na primeira vez que alguém
ajusta só um dos lados. Aqui `leitura_acordo_minimo` continua significando a mesma coisa
esteja a placa vindo do bico ou do monitoramento contínuo.

`consenso_caractere` morava em `leitura.py` e por isso o contínuo não tinha acesso a ela:
o tracker votava por STRING EXATA (`Counter` sobre a placa inteira) e três leituras da
mesma moto — `RLT2477`, `NLX2A77`, `RLX2A77` — davam zero concordância e o veículo saía
sem emitir, embora o voto por POSIÇÃO devolvesse `RLX2A77`, que era a placa certa e a
leitura de maior confiança do lote. Ver o caso de 24/08/2026 no bico 3 do ALTIPLANO.
"""
from __future__ import annotations

from collections import defaultdict

from app.visao.validador import parecidas, validar

# Posições esperadas por padrão de placa: L = letra, D = dígito. Espelha
# `validador.POSICOES_ANTIGO`/`POSICOES_MERCOSUL`, mas indexado pelo NOME do padrão porque
# aqui a entrada é o veredito do validador ('mercosul'/'antigo'), não uma lista de posições.
_PADRAO_POS = {"mercosul": "LLLDLDD", "antigo": "LLLDDDD"}


def pesos_por_posicao(peso) -> list[float]:
    """Normaliza o peso de UMA leitura para 7 valores, um por posição da placa.

    O peso que chega pode ser um `float` (confiança da leitura toda) ou uma sequência de 7
    (confiança por caractere, que o `fast_plate_ocr` expõe e os outros engines não). Existe
    como função porque QUATRO lugares deste módulo consomem peso — o voto, o prior de
    formato, o agrupamento e o acordo — e cada um resolvendo a união por conta própria é uma
    regra duplicada quatro vezes, que é o que este módulo existe para não ter.

    Piso de 0,01: peso zero apagaria a leitura do voto em vez de dar-lhe pouca força, e
    "engine devolveu confiança 0" não é o mesmo que "engine não leu".

    Vetor mais curto que 7 cai para a média replicada, e não para índice fora de faixa: o
    alinhamento já falhou antes (ver `engines._alinhar_por_char`), e aqui a escolha é
    degradar para o comportamento escalar.
    """
    if isinstance(peso, (int, float)):
        return [max(float(peso), 0.01)] * 7
    try:
        seq = [float(x) for x in peso]
    except (TypeError, ValueError):
        return [0.01] * 7
    if len(seq) < 7:
        media = sum(seq) / len(seq) if seq else 0.01
        return [max(media, 0.01)] * 7
    return [max(x, 0.01) for x in seq[:7]]


def peso_medio(peso) -> float:
    """Um número só a partir do peso de uma leitura — para quem compara leituras INTEIRAS.

    `agrupar_por_veiculo` e `prior_de_formato` decidem sobre a leitura como um todo (a que
    grupo pertence, qual formato ela sustenta), então peso por posição ali não tem sentido:
    o que vale é o quanto aquela leitura pesa. Média, e não máximo, para uma leitura com uma
    posição muito confiante e o resto ruim não valer o mesmo que uma leitura boa inteira.
    """
    pesos = pesos_por_posicao(peso)
    return sum(pesos) / len(pesos)


def _forca(pesos: list[float]) -> float:
    """Quanto um caractere candidato pesa numa posição, somando os QUADRADOS das confianças.

    Soma simples é o que parece óbvio e é o que estava aqui — e ela deixa duas leituras
    medíocres que concordam baterem uma muito confiante. Caso medido, `GAE0244` na posição 4
    (verdade `2`):

        '3' de DAE8343 ... 0,868
        '2' de GAE0244 ... 0,991   <- o correto, e o mais confiante de todos
        '3' de GAE0344 ... 0,432

    Soma: '3' = 1,300 contra 0,991 do '2', e sai `GAE0344`. Quadrados: '3' = 0,753 + 0,187 =
    0,940 contra 0,982 do '2', e sai `GAE0244`. Isto importa porque os três modelos do
    ensemble são da MESMA família: erro correlacionado é o caso comum, não a exceção, e a
    soma linear premia justamente a correlação.

    Quadrado e não máximo: `max` ignoraria concordância por completo, e foi pior na medição
    (31/40 contra 32/40 no dataset rotulado). Quadrado mantém a concordância valendo, só não
    deixa ela atropelar uma certeza isolada. Cubo mede igual ao quadrado e é mais agressivo
    sem ganho — não há razão para ir além.

    Ganho medido: +1 nos 7 recortes adjudicados por imagem, 0 de custo no dataset rotulado.
    Evidência de UM caso corrigido; não é base para afirmar mais do que isso.
    """
    return sum(x * x for x in pesos)


def consenso_caractere(leituras: list[tuple[str, float]], formato: str | None = None) -> str | None:
    """Consenso por POSIÇÃO de caractere, ponderado por confiança (padrão de mercado ALPR).

    Combina várias leituras (de múltiplos frames E engines) votando cada posição
    separadamente — corrige erros de 1 caractere: se 2 frames leem 'ABC1D23' e 1 lê
    'ABC1O23', a posição 5 elege 'D'. Considera só placas de 7 chars (padrão BR).

    `formato` ('mercosul'/'antigo'): quando o tipo visual é conhecido (faixa azul do
    Mercosul), restringe cada posição ao TIPO esperado — na posição 5 do Mercosul só
    conta votos de LETRA, descartando dígitos como erro de OCR (em vez de chutar 2→Z).
    Recupera a letra certa de outro frame. Se nenhuma leitura deu o tipo certo numa
    posição, cai para o voto bruto daquela posição.

    O peso de cada leitura pode ser um `float` (a confiança da leitura toda) OU uma
    sequência de 7 floats (a confiança POR POSIÇÃO). União de tipos, e não parâmetro novo,
    porque três chamadores só têm o escalar — tracker, `leitura._eleger_placa` e as leituras
    de PaddleOCR/EasyOCR, que não expõem confiança por caractere.

    Por que o peso por posição importa: com peso escalar, uma leitura vota com a MESMA força
    em todas as posições, inclusive nas que o próprio modelo marcou como incertas. Quando
    dois modelos da mesma família erram o mesmo caractere — e eles erram, é a mesma
    arquitetura no mesmo treino —, a maioria simples amplifica o erro em vez de corrigir.
    Medido em 5 recortes adjudicados por imagem: `GAE0244`→`GAE0344`, `NOF6758`→`NDF6758`,
    `OSE2923`→`QSE2923`, `OFV0025`→`QFV0025`, todos O→Q/O→D/2→3 com o banco certo e a fusão
    errada. Nos mesmos recortes o modelo já entregava a informação que faltava: em
    `BLX2677` as posições 1 e 4 vêm com 0,30 e 0,22 e são exatamente as duas erradas.
    """
    validas = [(p, pesos_por_posicao(w)) for p, w in leituras if p and len(p) == 7]
    if not validas:
        return None
    padrao = _PADRAO_POS.get(formato or "")
    consenso = []
    for i in range(7):
        votos: dict[str, list[float]] = defaultdict(list)
        if padrao:
            tipo = padrao[i]
            for p, pesos in validas:
                ch = p[i]
                if tipo == "L" and not ch.isalpha():
                    continue          # espera letra, veio dígito → descarta (erro)
                if tipo == "D" and not ch.isdigit():
                    continue          # espera dígito, veio letra → descarta
                votos[ch].append(pesos[i])
        if not votos:                 # sem formato, ou nenhum voto do tipo certo
            for p, pesos in validas:
                votos[p[i]].append(pesos[i])
        consenso.append(max(votos.items(), key=lambda kv: _forca(kv[1]))[0])
    return "".join(consenso)


# Distância máxima, em posições, para duas leituras contarem como o MESMO veículo dentro do
# pool de votação. É o mesmo `max_diff=2` de `validador.parecidas`, e pela mesma razão: 1-2
# caracteres é o ruído clássico de OCR (0/O/D/Q, I/1/J) sobre a mesma placa; 3+ é outra
# placa. Não é um limiar novo — é o que o histórico já usa para não duplicar linha.
MAX_DIFF_MESMO_VEICULO = 2


def agrupar_por_veiculo(leituras: list[tuple[str, float]]) -> list[list[tuple[str, float]]]:
    """Particiona o pool em grupos que plausivelmente são o MESMO veículo, maior primeiro.

    Existe porque votar caractere a caractere só faz sentido entre leituras da MESMA placa.
    O pool da leitura reativa mistura as câmeras do bico, e num posto a câmera da frente e a
    da traseira frequentemente enquadram VEÍCULOS DIFERENTES — moto no Brasil não tem placa
    dianteira, então na leitura de uma moto o que a frente vê é outro carro. Fundir os dois
    posição a posição não corrige ruído: fabrica uma terceira placa.

    Foi exatamente isso em 24/08/2026 (bico 3, ALTIPLANO): a traseira leu `OSL2G55` da moto,
    a frontal contribuiu um candidato de outro veículo, e a fusão emitiu `OSL2855` — string
    que engine NENHUM produziu — com `acordo=0.00`, gravada no histórico como leitura.

    Agrupamento por transitividade (união de vizinhos), não por par: A~B e B~C põem os três
    no mesmo grupo mesmo quando A e C diferem em 3. É o comportamento certo para ruído de
    OCR, que acumula ao longo de vários frames do mesmo veículo.
    """
    # PRESERVA o peso como veio. O grupo vencedor vai direto para `consenso_caractere`, e
    # colapsar o peso aqui (com `peso_medio`) jogaria fora a confiança POR POSIÇÃO antes de
    # ela ser usada — que é justamente o que ela existe para fazer. O `peso_medio` entra só
    # no critério de ORDENAÇÃO abaixo, onde a pergunta é sobre a leitura inteira.
    validas = [(p, w) for p, w in leituras if p and len(p) == 7]
    if not validas:
        return []
    grupos: list[list[tuple[str, float]]] = []
    for item in validas:
        casou = [g for g in grupos
                 if any(parecidas(item[0], p, MAX_DIFF_MESMO_VEICULO) for p, _ in g)]
        if not casou:
            grupos.append([item])
            continue
        # Une TODOS os grupos que este item aproxima — sem isto, dois grupos que só o item
        # atual liga ficariam separados e o "maior grupo" seria menor do que a evidência é.
        #
        # Descarte por IDENTIDADE (`is`) e não `grupos.remove(outro)`: `list.remove` compara
        # por CONTEÚDO, então dois grupos com as mesmas leituras fariam ele apagar o
        # primeiro igual da lista — possivelmente o `alvo`, que acabaria fora de `grupos`
        # levando embora a fusão inteira. Hoje isso não é alcançável (grupos em `casou` não
        # casam entre si, logo têm conteúdos diferentes), mas depender disso é depender de
        # um invariante a duas inferências de distância.
        alvo = casou[0]
        alvo.append(item)
        absorvidos = casou[1:]
        for outro in absorvidos:
            alvo.extend(outro)
        grupos[:] = [g for g in grupos if not any(g is o for o in absorvidos)]
    # Peso primeiro, tamanho como critério de desempate: duas leituras confiantes valem mais
    # que três fracas, que é a mesma regra que a votação por posição já aplica. `peso_medio`
    # porque a comparação é entre GRUPOS de leituras inteiras — somar vetor por vetor daria
    # um número que depende de quantas leituras têm confiança por caractere, não de quão boas
    # elas são.
    grupos.sort(key=lambda g: (sum(peso_medio(w) for _, w in g), len(g)), reverse=True)
    return grupos


def leitura_real_proxima(placa: str | None, leituras: list[tuple[str, float]]) -> bool:
    """A placa eleita tem respaldo em algo que um engine REALMENTE leu?

    `consenso_caractere` monta uma string caractere a caractere, então ela pode sair do
    processo sem nunca ter sido lida por ninguém. Quando o pool é do mesmo veículo isso é o
    objetivo (é assim que 12 leituras divergentes viram a placa certa); quando o pool está
    sujo, é invenção. A diferença mensurável é a distância até a leitura mais próxima:
    fusão legítima fica a 1-2 caracteres de várias leituras, invenção não fica perto de
    nenhuma.

    Não substitui `agrupar_por_veiculo` — é a segunda tranca, para o caso em que o grupo
    vencedor é ele mesmo heterogêneo.
    """
    if not placa:
        return False
    return any(parecidas(placa, p, MAX_DIFF_MESMO_VEICULO)
               for p, _ in leituras if p and len(p) == 7)


def prior_de_formato(leituras: list[tuple[str, float]]) -> str | None:
    """'mercosul' / 'antigo' / None — o formato predominante NAS LEITURAS QUE VAO VOTAR.

    O prior nao e cosmetico: `consenso_caractere` usa ele para RESTRINGIR cada posicao ao
    tipo esperado (a posicao 5 do Mercosul e LETRA), entao um prior errado nao "chuta
    diferente" — ele DESCARTA o voto certo. Medido: com prior 'antigo', a fusao de
    `HDX2477` com tres leituras `RLX2A77`/`NLX2A77` devolvia `RLX2477`, porque os votos em
    'A' eram jogados fora por serem letra.

    Daí as duas regras que esta funcao existe para nao deixar divergir:

    1. Sai das leituras que estao NO POOL, e nao de todas as que chegaram. Leitura que o
       agrupamento por veiculo excluiu nao deve opinar sobre o formato da placa vencedora.
    2. Ponderado por confianca, e nao contagem simples. Sem peso, uma leitura fraca e
       minoritaria fixa o prior e leva os votos bons com ela.

    Existia em TRES copias — `leitura._eleger_placa`, `ocr.auto.AutoOCR._fundir` e
    `tracker._EstadoTrack.placa_eleita` — e as tres ja tinham divergido entre si (uma
    contava sem peso, outra somava leitura fora do pool). E o mesmo motivo pelo qual este
    modulo existe.
    """
    votos: dict[str, float] = defaultdict(float)
    for placa, peso in leituras:
        v = validar(placa)
        if v:
            votos[v[1]] += peso_medio(peso)
    if not votos:
        return None
    return max(votos.items(), key=lambda kv: kv[1])[0]


def acordo_por_caractere(placa: str | None, leituras: list[tuple[str, float]]) -> float:
    """Concordancia MEDIA por posicao com `placa`, ponderada por confianca (0-1).

    Existe porque a medida antiga de `acordo` e casamento de STRING EXATA, e isso deixou de
    descrever o que o sistema faz no dia em que a placa passou a sair de uma fusao. O caso
    que expos o problema: tres leituras da mesma moto (`RLT2477`, `NLX2A77`, `RLX2A77`)
    fundem em `RLX2A77`, e o acordo por string exata da 1/3 = 0,33 - numero que sugere
    leitura ruim quando as tres leituras concordavam em 5 das 7 posicoes. Por caractere o
    mesmo caso da 0,86, que e o que a evidencia de fato mostra.

    NAO substitui a medida antiga por decisao: `leitura_acordo_minimo` esta calibrado em
    0,80 sobre a escala de string exata, e trocar a escala por baixo de um limiar calibrado
    move o ponto de corte de todas as leituras do posto de uma vez. As duas convivem e quem
    escolhe e `acordo_metrica`, com default na antiga - ver `app/core/config.py`.

    Devolve 0.0 sem placa ou sem leitura utilizavel: quem chama nunca deve ler ausencia de
    evidencia como concordancia perfeita, que e o erro que `confirmada` documenta.
    """
    if not placa or len(placa) != 7:
        return 0.0
    validas = [(p, pesos_por_posicao(w)) for p, w in leituras if p and len(p) == 7]
    if not validas:
        return 0.0
    peso_total = sum(sum(pesos) for _, pesos in validas)
    if peso_total <= 0:
        return 0.0
    de_acordo = sum(pesos[i] for p, pesos in validas
                    for i in range(7) if p[i] == placa[i])
    return de_acordo / peso_total


def confirmada(acordo: float, n_votos: int, acordo_min: float, n_min: int,
               n_fotos: int | None = None) -> bool:
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

    `n_fotos` (opcional) é a segunda tranca, e ela existe porque `n_votos` conta LEITURAS
    DE ENGINE, que podem vir todas da MESMA foto. Com o ensemble (3 fast + paddle) uma
    única foto rende 3-4 leituras e fecha `n_votos >= 2` sozinha — e as 4 olham o MESMO
    recorte, então concordam sobre um falso positivo do detector se houver um.

    Medido ao vivo em 01/09/2026 (campanha de 20 leituras no ALTIPLANO): das 4 chamadas em
    que a parada antecipada fecharia na 2ª foto, 3 tinham 2 fotos concordando e uma tinha
    `votos_snap=1, cands=1` — apoiada numa foto só. Exigir `n_fotos` barra exatamente essa
    e mantém as outras três.

    NÃO substitui `n_votos`: a regra das 2 leituras foi calibrada em 80 recortes reais
    contra 80 falsos positivos (86% das reais passam, 4% dos falsos), e a regra por FOTOS
    sozinha media 0% e 0% — com 1 foto no orçamento ela nunca fecha. As duas somam.

    Opcional (default `None` = não checa) porque o contínuo chama esta função com outra
    semântica de voto: em `pipeline.py` um voto JÁ É um frame distinto, então a tranca de
    fotos seria a mesma condição contada duas vezes. Só a leitura reativa passa o número.
    """
    if acordo < acordo_min or n_votos < min(2, n_min):
        return False
    if n_fotos is not None and n_fotos < min(2, n_min):
        return False
    return True
