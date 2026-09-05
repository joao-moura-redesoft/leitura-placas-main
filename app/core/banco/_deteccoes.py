"""Detecções de placa, listas branca/negra e retenção de dados."""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone

from ._base import _agora, inicio_do_dia_local

from ._base import cursor


# Conjuntos de origem que o histórico sabe filtrar. Não são os valores da coluna
# `origem` (esses são 'roteador'/'teste'/'pipeline'): 'producao' agrupa tudo que NÃO
# é teste manual, porque do ponto de vista de quem audita um abastecimento o que
# importa é "isto aconteceu de verdade" e não qual caminho de código gravou a linha.
ORIGENS_FILTRO = ("producao", "teste", "feira", "todas")

# Origens que NÃO contam como produção. 'teste' é o botão do painel; 'feira' é o modo de
# demonstração (app/visao/feira.py), em que a placa NÃO veio do OCR — é mock. As duas
# precisam ficar fora de 'producao' pelo mesmo motivo: são leitura que não aconteceu de
# verdade, e contá-las na taxa de acerto mede o instrumento, não o sistema.
#
# 'feira' é filtro de PRIMEIRA CLASSE (está em ORIGENS_FILTRO) de propósito. Excluí-la de
# 'producao' sem dar como listá-la a deixaria visível só em 'todas' — exatamente o buraco
# que o COALESCE abaixo existe para evitar, e que tornaria impossível auditar depois
# quantas leituras da feira foram mockadas.
_ORIGENS_NAO_PRODUCAO = ("teste", "feira")

# Quantos `?` cabem numa cláusula IN por vez. Bem abaixo do teto real do SQLite
# (`SQLITE_LIMIT_VARIABLE_NUMBER`, 32766 nesta build; 999 em builds antigas) para o código
# funcionar em qualquer uma delas, e pequeno o bastante para nenhuma transação segurar o
# write lock por muito tempo enquanto o pipeline tenta gravar detecção.
_LOTE_PARAMETROS = 500

# Tipos de veículo que o histórico sabe filtrar. 'desconhecido' é um filtro de PRIMEIRA
# CLASSE, e não um resto: NULL é o valor de toda linha em que o detector de veículo não
# rodou ou não achou veículo (medido: ~21% dos quadros reais), e sem esse filtro não haveria
# como listar justamente as linhas que ninguém consegue classificar. Fundi-las em 'carro'
# seria inventar dado.
TIPOS_VEICULO_FILTRO = ("moto", "carro", "desconhecido", "todos")

# Vocabulário de `tipo_veiculo_fonte` — o MOTIVO por trás do veredito de `tipo_veiculo`,
# inclusive quando ele é NULL (ver a nota da coluna em `app/core/banco/_esquema.py`).
# Cresce (o replay de `testes/recalcula_tipo_veiculo.py` usa o prefixo `replay:`), por
# isso é validado aqui em Python, e não por CHECK de coluna: um CHECK não dá para
# estender sem recriar a tabela.
TIPOS_VEICULO_FONTE = ("veiculo", "classe-nao-mapeada", "veiculo-ambiguo",
                       "sem-veiculo", "tiles", "sem-2-estagios", "track-sem-deteccao")


def _validar_tipo_veiculo(tipo_veiculo: str | None) -> None:
    if tipo_veiculo not in (None, "moto", "carro"):
        raise ValueError(f"tipo_veiculo inválido: {tipo_veiculo!r}")


def _validar_tipo_veiculo_fonte(fonte: str | None) -> None:
    if fonte is None or fonte in TIPOS_VEICULO_FONTE or fonte.startswith("replay:"):
        return
    raise ValueError(
        f"tipo_veiculo_fonte inválido: {fonte!r} "
        f"(use {', '.join(TIPOS_VEICULO_FONTE)}, ou o prefixo 'replay:')")


def _filtro_tipo_veiculo(tipo: str | None) -> str:
    """Fragmento SQL do filtro de tipo de veículo, sobre o alias `d` de `deteccoes`.

    Espelha `_filtro_origem`: valida contra a lista e levanta ValueError em valor
    inválido, em vez de devolver silenciosamente o conjunto errado.
    """
    if tipo is None or tipo == "todos":
        return ""
    if tipo not in TIPOS_VEICULO_FILTRO:
        raise ValueError(
            f"tipo_veiculo inválido: {tipo!r} (use {', '.join(TIPOS_VEICULO_FILTRO)})")
    if tipo == "desconhecido":
        return " AND d.tipo_veiculo IS NULL"
    # Sem o `IS NOT NULL` explícito o SQL já excluiria NULL, mas deixar escrito evita que
    # alguém "otimize" para NOT IN mais tarde e faça as linhas NULL sumirem de todo filtro.
    return " AND d.tipo_veiculo IS NOT NULL AND d.tipo_veiculo = ?"


def _filtro_origem(origem: str | None, incluir_testes: bool) -> str:
    """Fragmento SQL do filtro de origem, sobre o alias `d` de `deteccoes`.

    `incluir_testes` é o parâmetro antigo, mantido porque `/api/deteccoes` é
    documentado com ele (docs/documentacao.html) e pode haver integração usando.
    Ele é binário e por isso não consegue expressar "só os testes" — que é o caso de
    quem acabou de mexer num ROI e quer conferir. Quando `origem` vem preenchido é
    ele que manda; o booleano só decide o default.

    O COALESCE é defensivo e vem do código original: a migração que criou a coluna usa
    `NOT NULL DEFAULT 'roteador'` (app/core/banco/_esquema.py), então ela já preencheu as
    linhas antigas e NULL não deveria existir. Mantido porque custa nada e a falha seria
    silenciosa: em SQL, tanto `origem <> 'teste'` quanto `origem = 'teste'` dão NULL para
    uma linha NULL, então ela sumiria de 'producao' E de 'teste' — aparecendo só em
    'todas', que é justamente o filtro que ninguém usa para conferir.
    """
    if origem is None:
        origem = "todas" if incluir_testes else "producao"
    if origem not in ORIGENS_FILTRO:
        raise ValueError(f"origem inválida: {origem!r} (use {', '.join(ORIGENS_FILTRO)})")
    if origem == "producao":
        lista = ", ".join(f"'{o}'" for o in _ORIGENS_NAO_PRODUCAO)
        return f" AND COALESCE(d.origem, 'roteador') NOT IN ({lista})"
    if origem in ("teste", "feira"):
        return f" AND COALESCE(d.origem, 'roteador') = '{origem}'"
    return ""


def registrar_deteccao(
    placa: str,
    padrao: str,
    confianca: float,
    snapshot: str | None = None,
    camera_id: str | None = None,
    bbox: dict | None = None,
    bico_id: int | None = None,
    frame: str | None = None,
    origem: str = "roteador",
    camera_db_id: int | None = None,
    acordo: float | None = None,
    confirmada: bool | None = None,
    tipo_veiculo: str | None = None,
    veiculo_classe: int | None = None,
    veiculo_conf: float | None = None,
    tipo_veiculo_fonte: str | None = None,
) -> int:
    """Registra uma detecção.

    `acordo` é a fração das leituras independentes que apontaram esta placa (0..1) e
    `confirmada` é o veredito de `app/visao/consenso.py` sobre ela. As duas origens
    preenchem os dois campos — muda só o que conta como uma leitura: fotos do loop
    reject-retry na leitura reativa, passadas de OCR no mesmo veículo rastreado (ou
    frames consecutivos iguais) no pipeline ao vivo.

    Ficam None onde o consenso é de fato desconhecido: linhas gravadas antes desta
    marcação existir. Desconhecido nunca deve ser lido como confirmado — por isso a
    coluna admite NULL em vez de assumir um padrão.

    `tipo_veiculo` ('moto'/'carro'/None) é a ESTIMATIVA do detector de veículo, não um
    cadastro — ver a nota da coluna em `app/core/banco/_esquema.py`. None quando não há
    estimativa, nunca 'carro' por omissão: o histórico distingue "é carro" de "não sei".

    `veiculo_classe`/`veiculo_conf`/`tipo_veiculo_fonte` são o SINAL CRU por trás desse
    veredito — mesmo precedente de `acordo`+`confirmada`. Os quatro vêm sempre juntos de
    uma única `OrigemTipo` (`app/visao/detector.py`): quem chama não deve montá-los à mão.
    """
    _validar_tipo_veiculo(tipo_veiculo)
    _validar_tipo_veiculo_fonte(tipo_veiculo_fonte)
    with cursor() as c:
        cur = c.execute(
            "INSERT INTO deteccoes (placa, padrao, confianca, snapshot, criado_em, camera_id, "
            "bbox, bico_id, frame, origem, camera_db_id, acordo, confirmada, tipo_veiculo, "
            "veiculo_classe, veiculo_conf, tipo_veiculo_fonte) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (placa, padrao, confianca, snapshot, _agora(), camera_id,
             json.dumps(bbox) if bbox else None, bico_id, frame, origem, camera_db_id,
             acordo, None if confirmada is None else int(confirmada), tipo_veiculo,
             veiculo_classe, veiculo_conf, tipo_veiculo_fonte),
        )
        return cur.lastrowid


def ultima_deteccao_bico(bico_id: int, desde: str, origem: str) -> dict | None:
    """Última detecção deste bico (mesma origem) desde `desde` (ISO) — usado para
    mesclar leituras repetidas do mesmo veículo em vez de duplicar linha no histórico."""
    with cursor() as c:
        row = c.execute(
            "SELECT * FROM deteccoes WHERE bico_id=? AND origem=? AND criado_em>=? "
            "ORDER BY criado_em DESC LIMIT 1",
            (bico_id, origem, desde),
        ).fetchone()
        return dict(row) if row else None


def ultima_deteccao_camera(camera_db_id: int, desde: str, origem: str | None = None) -> dict | None:
    """Última detecção NESTA câmera física (qualquer bico/origem, salvo se `origem`
    filtrar) desde `desde` (ISO) — usado para cruzar detecções do 'pipeline' (que não
    têm bico_id) com leituras 'roteador'/'teste' da mesma câmera e evitar duplicar o
    mesmo veículo visto pelos dois mecanismos quase ao mesmo tempo."""
    sql = "SELECT * FROM deteccoes WHERE camera_db_id=? AND criado_em>=?"
    params: list = [camera_db_id, desde]
    if origem is not None:
        sql += " AND origem=?"
        params.append(origem)
    sql += " ORDER BY criado_em DESC LIMIT 1"
    with cursor() as c:
        row = c.execute(sql, params).fetchone()
        return dict(row) if row else None


def atualizar_deteccao(id_: int, *, placa: str, padrao: str, confianca: float,
                        snapshot: str | None = None, frame: str | None = None,
                        acordo: float | None = None,
                        confirmada: bool | None = None,
                        tipo_veiculo: str | None = None,
                        veiculo_classe: int | None = None,
                        veiculo_conf: float | None = None,
                        tipo_veiculo_fonte: str | None = None,
                        camera_db_id: int | None = None) -> bool:
    """Atualiza placa/padrão/confiança de uma detecção existente — usado ao mesclar uma
    leitura nova com a detecção anterior do mesmo bico em vez de criar uma 2ª linha.

    Também renova `criado_em` para AGORA: a janela de cooldown deve deslizar a cada
    retry parecido, senão uma sequência de retries do roteador mais longa que um único
    cooldown_seg (ex.: 3 chamadas 70s uma da outra = 140s de ponta a ponta) volta a
    duplicar linha na 3ª chamada mesmo todas sendo o mesmo veículo.
    """
    _validar_tipo_veiculo(tipo_veiculo)
    _validar_tipo_veiculo_fonte(tipo_veiculo_fonte)
    with cursor() as c:
        cur = c.execute(
            "UPDATE deteccoes SET placa=:placa, padrao=:padrao, confianca=:confianca, "
            "criado_em=:criado_em, "
            "snapshot=COALESCE(:snapshot, snapshot), frame=COALESCE(:frame, frame), "
            "acordo=COALESCE(:acordo, acordo), confirmada=COALESCE(:confirmada, confirmada), "
            # A câmera que de fato LEU esta chamada. Não estava na lista, e a linha ficava
            # com a câmera da leitura ANTERIOR: num bico de duas câmeras, a chamada seguinte
            # cruzava o pipeline pela câmera errada e o mesmo veículo aparecia duas vezes no
            # histórico — exatamente o que o comentário do INSERT diz estar prevenindo.
            # COALESCE porque quem não sabe a câmera (chamador antigo) não deve apagá-la.
            # (Auditoria 27/08/2026, achado M3.)
            "camera_db_id=COALESCE(:camera_db_id, camera_db_id), "
            # Os quatro campos do tipo de veículo se movem em BLOCO, e não por COALESCE
            # independente — `:fonte` é o discriminante de presença (nunca None numa
            # leitura real; todo caminho de `app/visao/detector.py` se rotula). Um COALESCE
            # por coluna deixaria linha incoerente: `tipo_veiculo` sobrevivendo da leitura
            # anterior enquanto `veiculo_classe` vem da nova — tipo de um veículo com sinal
            # cru de outro. Por isso este é o único statement do arquivo com parâmetro
            # NOMEADO em vez de posicional: é o que permite repetir `:fonte` nas 3 cláusulas.
            "tipo_veiculo = CASE WHEN :fonte IS NULL THEN tipo_veiculo ELSE :tipo END, "
            "veiculo_classe = CASE WHEN :fonte IS NULL THEN veiculo_classe ELSE :classe END, "
            "veiculo_conf = CASE WHEN :fonte IS NULL THEN veiculo_conf ELSE :conf END, "
            "tipo_veiculo_fonte = COALESCE(:fonte, tipo_veiculo_fonte) "
            "WHERE id=:id",
            {
                "placa": placa, "padrao": padrao, "confianca": confianca,
                "criado_em": _agora(), "snapshot": snapshot, "frame": frame,
                "acordo": acordo,
                "confirmada": None if confirmada is None else int(confirmada),
                "camera_db_id": camera_db_id,
                "fonte": tipo_veiculo_fonte, "tipo": tipo_veiculo,
                "classe": veiculo_classe, "conf": veiculo_conf,
                "id": id_,
            },
        )
        return cur.rowcount > 0


def contar_deteccoes_placa(placa: str, incluir_testes: bool = False,
                            empresa_id: int | None = None,
                            origem: str | None = None,
                            tipo_veiculo: str | None = None) -> int:
    """Total de detecções de uma placa EXATA — sem o teto de `limit`.

    A consulta de placa fazia LIKE com limite de 50 e depois filtrava a igualdade em
    Python: o total saturava em 50 e, entre placas parecidas, as exatas podiam nem
    caber nas 50 primeiras linhas.

    `empresa_id`: escopa ao posto de um usuário 'cliente' (deps.py:empresa_do_usuario) —
    precisa do mesmo JOIN via bico→automação que `listar_deteccoes` usa, senão a
    contagem batia com todas as empresas e a listada (escopada) batia só com a dele.
    """
    sql = ("SELECT COUNT(*) FROM deteccoes d "
           "LEFT JOIN bicos b ON d.bico_id = b.id "
           "LEFT JOIN automacoes a ON b.automacao_id = a.id "
           "WHERE d.placa=?")
    params: list = [placa]
    sql += _filtro_origem(origem, incluir_testes)
    # O filtro precisa valer aqui também: sem isso o total do cabeçalho contaria TODAS as
    # leituras da placa enquanto a lista mostraria só as de um tipo, e os dois números na
    # mesma tela se contradiriam.
    frag_tipo = _filtro_tipo_veiculo(tipo_veiculo)
    sql += frag_tipo
    if "?" in frag_tipo:
        params.append(tipo_veiculo)
    if empresa_id is not None:
        sql += " AND a.empresa_id = ?"
        params.append(empresa_id)
    with cursor() as c:
        return c.execute(sql, params).fetchone()[0]


def listar_deteccoes(
    placa: str | None = None,
    desde: str | None = None,
    ate: str | None = None,
    limit: int = 50,
    offset: int = 0,
    empresa_id: int | None = None,
    bico_id: int | None = None,
    incluir_testes: bool = False,
    placa_exata: bool = False,
    origem: str | None = None,
    tipo_veiculo: str | None = None,
) -> list[dict]:
    """Detecções com o posto/bico de origem resolvidos (LEFT JOIN — leituras antigas,
    anteriores ao multi-tenant, não têm bico e aparecem com os campos vazios).

    `placa_exata` troca o LIKE por igualdade — a busca da interface quer o LIKE
    (digitar parte da placa), a consulta de uma placa específica quer só ela.

    `tipo_veiculo`: 'moto' | 'carro' | 'desconhecido' | 'todos' (ver
    `TIPOS_VEICULO_FILTRO`). É a estimativa do detector de veículo, não cadastro.
    """
    sql = """
        SELECT d.*, b.codigo AS bico_codigo, b.nome AS bico_nome,
               em.id AS empresa_id, em.nome AS empresa_nome, em.cnpj AS empresa_cnpj
        FROM deteccoes d
        LEFT JOIN bicos b      ON d.bico_id = b.id
        LEFT JOIN automacoes a ON b.automacao_id = a.id
        LEFT JOIN empresas em  ON a.empresa_id = em.id
        WHERE 1=1
    """
    params: list = []
    # Testes ficam fora por padrão: são leituras disparadas por quem está configurando,
    # não abastecimentos, e inflariam a contagem do posto.
    sql += _filtro_origem(origem, incluir_testes)
    frag_tipo = _filtro_tipo_veiculo(tipo_veiculo)
    sql += frag_tipo
    if "?" in frag_tipo:
        params.append(tipo_veiculo)
    if placa:
        if placa_exata:
            sql += " AND d.placa = ?"
            params.append(placa)
        else:
            sql += " AND d.placa LIKE ?"
            params.append(f"%{placa}%")
    if desde:
        sql += " AND d.criado_em >= ?"
        params.append(desde)
    if ate:
        sql += " AND d.criado_em <= ?"
        params.append(ate)
    if empresa_id is not None:
        sql += " AND em.id = ?"
        params.append(empresa_id)
    if bico_id is not None:
        sql += " AND d.bico_id = ?"
        params.append(bico_id)
    # Desempate por id: `criado_em` tem resolução de microssegundo, mas duas detecções
    # do mesmo instante (duas câmeras, mesmo pulso) empatavam e saíam em ordem indefinida
    # — inclusive trocando de lugar entre duas páginas do histórico e escondendo uma linha.
    sql += " ORDER BY d.criado_em DESC, d.id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with cursor() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def remover_deteccao(id_: int) -> list[str] | None:
    """Apaga a linha e devolve os caminhos relativos dos JPEGs que ficaram órfãos
    (lista possivelmente vazia), ou `None` se não havia nada com esse id.

    Devolve os arquivos porque antes não devolvia: a linha sumia e o JPEG ficava em disco
    para sempre. Órfão é pior do que só ocupar espaço — ele é invisível para
    `imagens_excedentes`, que ancora o teto de contagem no banco, então nenhuma limpeza
    automática jamais o alcança. Quem chama (app/web/api.py) apaga do disco.

    `list[str] | None` em vez de bool: a rota precisa distinguir "não existia" (404) de
    "existia e não tinha foto" (204 com lista vazia), e `[] or None` colapsaria os dois.
    """
    with cursor() as c:
        linha = c.execute(
            "SELECT snapshot, frame FROM deteccoes WHERE id=?", (id_,)
        ).fetchone()
        if linha is None:
            return None
        c.execute("DELETE FROM deteccoes WHERE id=?", (id_,))
        return [v for v in (linha["snapshot"], linha["frame"]) if v]


def empresa_da_imagem(url_rel: str):
    """empresa_id dona do JPEG servido em `url_rel`, ou `None` se ninguém o referencia.

    Existe para escopar `/static/snapshots/` por posto. O arquivo não carrega o dono no
    nome (`{ts}_{PLACA}.jpg`), então quem sabe de quem ele é são as linhas de `deteccoes`
    que o apontam -- em `snapshot` (o recorte da placa) ou em `frame` (o quadro inteiro).

    `None` cobre DOIS casos que a rota trata igual (404): URL que nenhuma detecção
    referencia, e detecção cuja empresa não se resolve (contínuo em câmera sem posto). O
    aberto por omissão seria voltar ao vazamento que esta função existe para fechar.
    """
    with cursor() as c:
        linha = c.execute(
            f"SELECT {_EMPRESA_DETECCAO} AS empresa_id FROM deteccoes d "
            f"{_JOIN_EMPRESA_DETECCAO} WHERE d.snapshot = ? OR d.frame = ? LIMIT 1",
            (url_rel, url_rel),
        ).fetchone()
    return linha["empresa_id"] if linha else None


def _corte(dias: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat()


# JOIN que resolve a empresa "dona" de uma detecção: pelo bico (leitura reativa/teste)
# ou, faltando isso, pela câmera (detecção do modo contínuo, que não tem bico_id).
_JOIN_EMPRESA_DETECCAO = """
    LEFT JOIN bicos b       ON d.bico_id = b.id
    LEFT JOIN automacoes au ON b.automacao_id = au.id
    LEFT JOIN cameras cam   ON d.camera_db_id = cam.id
"""
_EMPRESA_DETECCAO = "COALESCE(au.empresa_id, cam.empresa_id)"


def deteccoes_e_chamadas_antigas(dias: int) -> dict:
    """Apaga `deteccoes`/`chamadas` antigas e devolve os caminhos relativos dos JPEGs
    (snapshot + frame) que ficaram órfãos.

    `dias` é o prazo PADRÃO. Empresas com `retencao_dias_override` preenchido (LGPD por
    cliente — ver `empresas_definir_retencao`) usam o prazo próprio em vez do padrão;
    as demais (override NULL) caem no padrão, exatamente como antes deste mecanismo.

    Só mexe no banco — apagar os arquivos em disco é responsabilidade de quem chama
    (app/operacao/retencao.py), pra esta camada não fazer I/O de arquivo. Sem alguma
    rotina de retenção, `deteccoes`/`chamadas` e os JPEGs em app/web/static/snapshots/
    crescem para sempre num servidor multi-tenant de longa duração.
    """
    arquivos: list[str] = []
    n_det = n_cham = 0
    with cursor() as c:
        overrides = {r["id"]: r["retencao_dias_override"] for r in c.execute(
            "SELECT id, retencao_dias_override FROM empresas "
            "WHERE retencao_dias_override IS NOT NULL"
        ).fetchall()}

        # 1) Empresas com prazo próprio — uma passada por empresa (lista tipicamente
        # pequena: só quem pediu prazo diferente do padrão).
        for emp_id, dias_emp in overrides.items():
            corte_emp = _corte(dias_emp)
            linhas = c.execute(
                f"SELECT d.snapshot, d.frame FROM deteccoes d {_JOIN_EMPRESA_DETECCAO} "
                f"WHERE d.criado_em < ? AND {_EMPRESA_DETECCAO} = ?",
                (corte_emp, emp_id),
            ).fetchall()
            arquivos += [r["snapshot"] for r in linhas if r["snapshot"]]
            arquivos += [r["frame"] for r in linhas if r["frame"]]
            n_det += c.execute(
                f"DELETE FROM deteccoes WHERE id IN ("
                f"  SELECT d.id FROM deteccoes d {_JOIN_EMPRESA_DETECCAO} "
                f"  WHERE d.criado_em < ? AND {_EMPRESA_DETECCAO} = ?)",
                (corte_emp, emp_id),
            ).rowcount
            n_cham += c.execute(
                "DELETE FROM chamadas WHERE criado_em < ? AND empresa_id = ?",
                (corte_emp, emp_id),
            ).rowcount

        # 2) Todo o resto (sem override, inclusive detecções sem empresa resolvida —
        # ex.: leituras antigas pré-multi-tenant, ou de teste, sem bico/câmera) usa o
        # prazo padrão. `dias <= 0` = padrão global desativado ("nunca apaga") — mas os
        # overrides do passo 1 já rodaram, então um prazo específico por cliente
        # continua valendo mesmo com o padrão global desligado.
        #
        # IS NULL em vez de COALESCE(...,-1) NOT IN (...): com COALESCE, uma detecção
        # sem empresa resolvida virava -1, e se não houvesse NENHUM override cadastrado
        # o placeholder de "nenhuma empresa a excluir" TAMBÉM era -1 — a comparação
        # `-1 NOT IN (-1)` dava falso e a detecção nunca era apagada pelo prazo padrão
        # (bug real, pego pelo teste `test_apaga_o_que_passou_do_prazo_...`).
        if dias > 0:
            corte_padrao = _corte(dias)
            if overrides:
                marcadores = ",".join("?" * len(overrides))
                filtro = f"AND ({_EMPRESA_DETECCAO} IS NULL OR {_EMPRESA_DETECCAO} NOT IN ({marcadores}))"
                filtro_cham = f"AND (empresa_id IS NULL OR empresa_id NOT IN ({marcadores}))"
                params_extra = tuple(overrides)
            else:
                filtro = filtro_cham = ""
                params_extra = ()
            linhas = c.execute(
                f"SELECT d.snapshot, d.frame FROM deteccoes d {_JOIN_EMPRESA_DETECCAO} "
                f"WHERE d.criado_em < ? {filtro}",
                (corte_padrao, *params_extra),
            ).fetchall()
            arquivos += [r["snapshot"] for r in linhas if r["snapshot"]]
            arquivos += [r["frame"] for r in linhas if r["frame"]]
            n_det += c.execute(
                f"DELETE FROM deteccoes WHERE id IN ("
                f"  SELECT d.id FROM deteccoes d {_JOIN_EMPRESA_DETECCAO} "
                f"  WHERE d.criado_em < ? {filtro})",
                (corte_padrao, *params_extra),
            ).rowcount
            n_cham += c.execute(
                f"DELETE FROM chamadas WHERE criado_em < ? {filtro_cham}",
                (corte_padrao, *params_extra),
            ).rowcount

    return {"arquivos": arquivos, "deteccoes_removidas": n_det, "chamadas_removidas": n_cham}


def contagem_com_imagem() -> int:
    """Quantas linhas de `deteccoes` ainda têm foto (`snapshot` ou `frame` não-nulos).

    Checagem BARATA para quem só quer saber "há algo a purgar?" antes de pagar o custo
    de `rotulos.protegidos()` (leitura de disco + parse de JSON) — ver
    `app/operacao/retencao.py::_purgar_por_contagem`, chamada a cada 5 minutos. Mesma
    condição WHERE de `imagens_excedentes`, só que sem o OFFSET nem a mutação.
    """
    with cursor() as c:
        return c.execute(
            "SELECT COUNT(*) FROM deteccoes WHERE snapshot IS NOT NULL OR frame IS NOT NULL"
        ).fetchone()[0]


def imagens_excedentes(max_leituras: int) -> dict:
    """Tira a FOTO das leituras que passaram do teto de contagem, da mais antiga para a mais
    nova, e devolve os caminhos relativos dos JPEGs que ficaram órfãos.

    A LINHA FICA. Só `snapshot`/`frame` viram NULL — placa, hora, bico e confiança continuam
    no histórico e nos relatórios. É a diferença central para `deteccoes_e_chamadas_antigas`,
    que apaga a linha inteira: aqui o que sobra em disco é o problema (221 MB para 971
    leituras, medido), não a linha (~200 bytes). `historico.html` já renderiza "—" quando as
    duas colunas são nulas, então isso não produz miniatura quebrada.

    Como em `deteccoes_e_chamadas_antigas`, apagar os arquivos é responsabilidade de quem
    chama (app/operacao/retencao.py) — esta camada não faz I/O de arquivo.
    """
    if max_leituras <= 0:                 # 0 = sem teto; só o prazo em dias vale
        return {"arquivos": [], "leituras_afetadas": 0}

    with cursor() as c:
        # O ORDER BY é IDÊNTICO ao de `listar_deteccoes` (inclusive o desempate por id DESC).
        # Tem de ser: "as N mais recentes" que sobrevivem à purga precisam ser exatamente as
        # N primeiras que a tela mostra, senão a purga come uma linha visível na página 1 e
        # poupa outra da página 3. O índice idx_deteccoes_criado(criado_em DESC) cobre.
        linhas = c.execute(
            "SELECT id, snapshot, frame FROM deteccoes "
            "WHERE snapshot IS NOT NULL OR frame IS NOT NULL "
            "ORDER BY criado_em DESC, id DESC LIMIT -1 OFFSET ?",
            (max_leituras,),
        ).fetchall()
        if not linhas:
            return {"arquivos": [], "leituras_afetadas": 0}

        arquivos = [r["snapshot"] for r in linhas if r["snapshot"]]
        arquivos += [r["frame"] for r in linhas if r["frame"]]

        # NÃO dá para usar `atualizar_deteccao`: ela é COALESCE(:snapshot, snapshot), ou
        # seja, é incapaz de gravar NULL por construção.
        #
        # EM LOTES, e não um `IN (?,?,…)` com o excedente inteiro. O limite desta build é
        # `SQLITE_LIMIT_VARIABLE_NUMBER = 32766` (medido), e passar disso levanta
        # `too many SQL variables`. O erro era capturado pelo laço de retenção e repetido a
        # cada 5 minutos PARA SEMPRE — o teto nunca era aplicado e o disco enchia em
        # silêncio. Alcançável ao ligar `retencao_max_imagens` num banco já grande.
        # (Auditoria 27/08/2026, achado A9.)
        ids = [r["id"] for r in linhas]
        for i in range(0, len(ids), _LOTE_PARAMETROS):
            lote = ids[i:i + _LOTE_PARAMETROS]
            marcadores = ",".join("?" * len(lote))
            c.execute(
                f"UPDATE deteccoes SET snapshot=NULL, frame=NULL WHERE id IN ({marcadores})",
                lote,
            )

    return {"arquivos": arquivos, "leituras_afetadas": len(linhas)}


def stats(fuso: str = "America/Sao_Paulo") -> dict:
    """Contadores do dashboard. `fuso` decide onde o dia começa — ver
    `_base.inicio_do_dia_local` e o achado M2 da auditoria de 27/08/2026."""
    corte_hoje = inicio_do_dia_local(fuso)
    with cursor() as c:
        total = c.execute("SELECT COUNT(*) FROM deteccoes").fetchone()[0]
        hoje = c.execute(
            "SELECT COUNT(*) FROM deteccoes WHERE criado_em >= ?", (corte_hoje,)
        ).fetchone()[0]
        top = [
            dict(r)
            for r in c.execute(
                "SELECT placa, COUNT(*) as ocorrencias FROM deteccoes "
                "GROUP BY placa ORDER BY ocorrencias DESC LIMIT 10"
            ).fetchall()
        ]
        return {"total": total, "hoje": hoje, "top": top}


def listas_listar(tipo: str | None = None, empresa_id: int | None = None,
                  limit: int = 1000) -> list[dict]:
    """Entradas de lista branca/negra visíveis para este escopo.

    `empresa_id=None` (admin) traz tudo. Com escopo, traz as GLOBAIS (empresa_id NULL) mais
    as do próprio posto — nunca as de outro. `limit` existe porque a rota é aberta ao painel
    e a tabela não tinha teto nenhum.
    """
    sql = "SELECT * FROM listas_placas"
    where: list[str] = []
    params: list = []
    if tipo:
        where.append("tipo=?")
        params.append(tipo)
    if empresa_id is not None:
        where.append("(empresa_id IS NULL OR empresa_id=?)")
        params.append(empresa_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY criado_em DESC LIMIT ?"
    params.append(limit)
    with cursor() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def listas_inserir(placa: str, tipo: str, descricao: str = "",
                   empresa_id: int | None = None) -> int:
    with cursor() as c:
        cur = c.execute(
            "INSERT INTO listas_placas (placa, tipo, descricao, criado_em, empresa_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (placa, tipo, descricao, _agora(), empresa_id),
        )
        return cur.lastrowid


def listas_obter(id_: int) -> dict | None:
    """Uma entrada por id — para o chamador conferir o dono antes de apagar."""
    with cursor() as c:
        r = c.execute("SELECT * FROM listas_placas WHERE id=?", (id_,)).fetchone()
        return dict(r) if r else None


def listas_remover(id_: int) -> bool:
    with cursor() as c:
        cur = c.execute("DELETE FROM listas_placas WHERE id=?", (id_,))
        return cur.rowcount > 0


def listas_buscar(placa: str, empresa_id: int | None = None) -> dict | None:
    """A entrada que vale para esta placa NESTE escopo.

    Com `empresa_id`, considera as globais e as do próprio posto. A do posto tem
    PRECEDÊNCIA sobre a global: quem cadastrou algo específico para o próprio pátio quis
    justamente sobrepor a regra geral.
    """
    with cursor() as c:
        if empresa_id is None:
            r = c.execute("SELECT * FROM listas_placas WHERE placa=?", (placa,)).fetchone()
        else:
            r = c.execute(
                "SELECT * FROM listas_placas WHERE placa=? "
                "AND (empresa_id IS NULL OR empresa_id=?) "
                "ORDER BY empresa_id IS NULL LIMIT 1",
                (placa, empresa_id),
            ).fetchone()
        return dict(r) if r else None
