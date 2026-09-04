"""Modo feira: substitui a leitura pelos veículos de demonstração cadastrados.

Para que serve
--------------
Numa feira, o carrinho de demonstração tem placa CONHECIDA. O modo faz a leitura dele
fechar sempre, sem depender de o OCR acertar uma mini-placa de poucos pixels — e sem
impedir que um visitante teste a placa do próprio celular, que continua passando pelo
caminho real.

Como o mock "prevalece" sem quebrar o resto
-------------------------------------------
O casamento roda DEPOIS da eleição e da fusão, sobre a placa já eleita. Isso é o que
permite sobrepor até uma leitura real confiante que errou um caractere — o requisito
"mockado prevalece" — sem encostar em detector, OCR, consenso ou merge.

Por que distância de edição e não igualdade
-------------------------------------------
Se exigisse igualdade, o modo nunca dispararia: o ponto é justamente a mini-placa sair
mal lida. Com tolerância 2 sobre 7 caracteres, a placa do carrinho fecha mesmo torta, e
uma placa arbitrária de visitante continua a uma distância enorme de qualquer placa
cadastrada — que é o que faz o celular do cliente NÃO ser sequestrado pelo mock.

O que este módulo deliberadamente NÃO faz
-----------------------------------------
Não decide sozinho que a leitura é mock e segue em silêncio. Ele só RESPONDE "esta placa
lida corresponde ao carrinho X". Marcar `avisos`, logar em WARNING e gravar com
`origem="feira"` é responsabilidade de quem chama, e não é opcional: leitura mockada é
dado sintético, e dado sintético já inverteu o sinal de uma medição neste projeto.
"""
from __future__ import annotations

import re

# Valor de `origem` que marca leitura MOCKADA — no banco (`deteccoes.origem`), no payload
# do roteador e no bloco `veiculo`. Constante, e não a string solta em cada ponto, porque
# é ela que mantém a leitura de demonstração FORA do filtro 'producao' e fora da taxa de
# acerto: um typo num dos pontos faria dado sintético entrar na medição em silêncio.
ORIGEM = "feira"


def normalizar(placa: str | None) -> str:
    """Sobe para maiúsculas e joga fora tudo que não é letra/dígito.

    `MOK-3H92`, `mok 3h92` e `MOK3H92` são a mesma placa para qualquer humano, e o
    operador vai digitar o hífen no cadastro porque é assim que a placa é impressa.
    """
    return re.sub(r"[^A-Z0-9]", "", (placa or "").upper())


def placas_demo(cfg: dict) -> list[str]:
    """As placas de demonstração cadastradas, normalizadas e sem vazias/duplicadas."""
    cru = (cfg.get("feira_placas") or "").split(",")
    vistas: list[str] = []
    for p in cru:
        n = normalizar(p)
        if n and n not in vistas:
            vistas.append(n)
    return vistas


def empresa_demo(cfg: dict) -> int | None:
    """Id do posto de demonstração, ou None se não há um.

    None significa mock DESARMADO, não "vale para todos" — ver `ativo`.
    """
    bruto = (cfg.get("feira_empresa_id") or "").strip()
    try:
        return int(bruto) if bruto else None
    except ValueError:
        # Config digitada à mão não pode virar um escopo que não existe e, pior, casar
        # com um posto qualquer por acidente. Valor ilegível = desarmado.
        return None


def ativo(cfg: dict) -> bool:
    """Ligado, com ao menos uma placa cadastrada E com posto de demonstração definido.

    As três juntas de propósito. As duas primeiras porque `feira_ativo=sim` com a lista
    vazia é um modo que não pode fazer nada, e tratá-lo como ativo só serviria para a
    faixa na tela mentir que a demo está armada.

    A terceira é a que importa para a segurança: SEM posto de demonstração o mock fica
    DESARMADO em vez de global. É fail-closed — ligar o interruptor num servidor que
    atende clientes reais não pode, sozinho, começar a mockar leitura de posto de
    verdade. O escopo é o que deixa a demo conviver com a produção.
    """
    return ((cfg.get("feira_ativo") or "").strip().lower() in ("sim", "true", "1")
            and bool(placas_demo(cfg))
            and empresa_demo(cfg) is not None)


def _distancia(a: str, b: str) -> int:
    """Levenshtein. Cobre substituição, inserção e remoção.

    Não é Hamming (só substituição) porque o OCR nem sempre devolve 7 caracteres: ele
    perde ou inventa um com alguma frequência em placa pequena, e é exatamente esse o
    caso que o modo existe para socorrer.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    anterior = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        atual = [i]
        for j, cb in enumerate(b, 1):
            atual.append(min(anterior[j] + 1,        # remoção
                             atual[j - 1] + 1,       # inserção
                             anterior[j - 1] + (ca != cb)))   # substituição
        anterior = atual
    return anterior[-1]


def casar(placa_lida: str | None, cfg: dict, empresa_id: int | None = None) -> str | None:
    """A placa de demonstração correspondente, ou None se não for veículo de demo.

    None significa "siga o caminho normal" — é o que acontece com a placa do celular do
    visitante, e é o desfecho padrão para tudo que não é o carrinho.

    `empresa_id` é o POSTO de onde veio a leitura. Só o posto de demonstração
    (`feira_empresa_id`) pode ser mockado; qualquer outro sai daqui com None por mais
    parecida que a placa esteja. É o que impede o modo de contaminar cliente real numa
    instalação que atende os dois.

    O default `None` existe para os testes puros de casamento ficarem legíveis, mas
    `None` NÃO é curinga: ele não bate com nenhum `feira_empresa_id`, então uma chamada
    que "esqueceu" de passar o posto falha fechada em vez de mockar tudo.

    EMPATE NÃO CASA: se a leitura fica à mesma distância de duas placas cadastradas, o
    modo desiste em vez de escolher. Duas placas de demo parecidas é erro de cadastro, e
    chutar entre elas produziria exatamente o resultado que a feira não pode ter — o
    carrinho A exibindo a placa do carrinho B, com confiança 1.0.
    """
    if not ativo(cfg):
        return None
    if empresa_id is None or empresa_id != empresa_demo(cfg):
        return None
    lida = normalizar(placa_lida)
    if not lida:
        # Sem string nenhuma não há o que casar. É o buraco conhecido do snap (placa
        # pequena demais para o OCR devolver qualquer coisa) e o motivo de `feira_marcadores`
        # existir como fase 2.
        return None
    try:
        tolerancia = int(cfg.get("feira_tolerancia", "2"))
    except (TypeError, ValueError):
        tolerancia = 2

    ranking = sorted(((_distancia(lida, alvo), alvo) for alvo in placas_demo(cfg)),
                     key=lambda par: par[0])
    melhor, alvo = ranking[0]
    if melhor > tolerancia:
        return None
    if len(ranking) > 1 and ranking[1][0] == melhor:
        return None      # empate — ver docstring
    return alvo
