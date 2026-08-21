"""Cadastro: câmeras e a árvore entidade → empresa(CNPJ) → automação → bico."""
from __future__ import annotations
import json
import sqlite3

from ._base import _agora, _normalizar_codigo

from ._base import cursor


# ─── Câmeras ───────────────────────────────────────────────────────────────
# Registro de CONEXÃO física (host/credenciais/tipo) — não carrega mais bomba/lado/roi,
# que agora pertencem ao bico (ver seção "Cadastro multi-tenant" abaixo).

def cameras_listar(empresa_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM cameras"
    params: list = []
    if empresa_id is not None:
        sql += " WHERE empresa_id=?"
        params.append(empresa_id)
    sql += " ORDER BY local, nome"
    with cursor() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def cameras_obter(id_: int) -> dict | None:
    with cursor() as c:
        r = c.execute("SELECT * FROM cameras WHERE id=?", (id_,)).fetchone()
        return dict(r) if r else None


def cameras_inserir(dados: dict) -> int:
    with cursor() as c:
        cur = c.execute(
            """INSERT INTO cameras
               (nome, empresa_id, local, camera_tipo, camera_indice,
                intelbras_host, intelbras_porta, intelbras_usuario, intelbras_senha,
                intelbras_canal, intelbras_subtype, intelbras_formato,
                rtsp_url_custom, ativo, criado_em)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                dados["nome"],
                int(dados["empresa_id"]) if dados.get("empresa_id") else None,
                dados.get("local", ""),
                dados.get("camera_tipo", "rtsp"),
                dados.get("camera_indice", "0"),
                dados.get("intelbras_host", ""),
                dados.get("intelbras_porta", "554"),
                dados.get("intelbras_usuario", "admin"),
                dados.get("intelbras_senha", ""),
                dados.get("intelbras_canal", "1"),
                dados.get("intelbras_subtype", "1"),
                dados.get("intelbras_formato", "padrao"),
                dados.get("rtsp_url_custom", ""),
                1 if dados.get("ativo", True) else 0,
                _agora(),
            ),
        )
        return cur.lastrowid


def cameras_atualizar(id_: int, dados: dict) -> bool:
    # Preserva senha armazenada quando o campo vier vazio (UI mascara)
    senha = dados.get("intelbras_senha") or ""
    if not senha:
        atual = cameras_obter(id_)
        senha = atual["intelbras_senha"] if atual else ""
    with cursor() as c:
        cur = c.execute(
            """UPDATE cameras SET
               nome=?, empresa_id=?, local=?, camera_tipo=?, camera_indice=?,
               intelbras_host=?, intelbras_porta=?, intelbras_usuario=?, intelbras_senha=?,
               intelbras_canal=?, intelbras_subtype=?, intelbras_formato=?,
               rtsp_url_custom=?, ativo=?
               WHERE id=?""",
            (
                dados["nome"],
                int(dados["empresa_id"]) if dados.get("empresa_id") else None,
                dados.get("local", ""),
                dados.get("camera_tipo", "rtsp"),
                dados.get("camera_indice", "0"),
                dados.get("intelbras_host", ""),
                dados.get("intelbras_porta", "554"),
                dados.get("intelbras_usuario", "admin"),
                senha,
                dados.get("intelbras_canal", "1"),
                dados.get("intelbras_subtype", "1"),
                dados.get("intelbras_formato", "padrao"),
                dados.get("rtsp_url_custom", ""),
                1 if dados.get("ativo", True) else 0,
                id_,
            ),
        )
        return cur.rowcount > 0


def cameras_remover(id_: int) -> bool:
    with cursor() as c:
        cur = c.execute("DELETE FROM cameras WHERE id=?", (id_,))
        return cur.rowcount > 0


# ─── Cadastro multi-tenant: entidade → empresa(CNPJ) → automação → bico ────
# Cadastro 100% manual (sem replicação). `resolver_bico` é o coração da leitura
# reativa: localiza câmera+ROI a partir de (cnpj, automacao, bico), reportando
# em qual nível o cadastro falhou (mensagem específica pro time de campo).

def entidades_listar() -> list[dict]:
    with cursor() as c:
        return [dict(r) for r in c.execute("SELECT * FROM entidades ORDER BY nome").fetchall()]


def entidades_obter(id_: int) -> dict | None:
    with cursor() as c:
        r = c.execute("SELECT * FROM entidades WHERE id=?", (id_,)).fetchone()
        return dict(r) if r else None


def entidades_inserir(dados: dict) -> int:
    with cursor() as c:
        cur = c.execute(
            "INSERT INTO entidades (nome, ativo, criado_em) VALUES (?,?,?)",
            (dados["nome"], 1 if dados.get("ativo", True) else 0, _agora()),
        )
        return cur.lastrowid


def entidades_atualizar(id_: int, dados: dict) -> bool:
    with cursor() as c:
        cur = c.execute(
            "UPDATE entidades SET nome=?, ativo=? WHERE id=?",
            (dados["nome"], 1 if dados.get("ativo", True) else 0, id_),
        )
        return cur.rowcount > 0


def _apagar_empresas(c: sqlite3.Connection, where_sql: str, params: tuple) -> None:
    """Apaga empresas e seus dependentes na ORDEM correta.

    Não dá para confiar só no ON DELETE CASCADE aqui: `bicos.camera_id` é RESTRICT
    (proteção contra apagar uma câmera em uso) e, quando o cascade tenta remover a
    câmera antes do bico que a usa, o RESTRICT dispara e o DELETE inteiro falha com
    "FOREIGN KEY constraint failed". Removendo bico → automação → câmera → empresa
    explicitamente, o RESTRICT continua protegendo a exclusão avulsa de câmera.
    """
    emp = f"SELECT id FROM empresas WHERE {where_sql}"
    # Usuários 'cliente' deste posto ficam órfãos (empresa_id vira NULL por ON DELETE
    # SET NULL) — desativa explicitamente em vez de deixá-los soltos: um usuário
    # 'cliente' sem empresa_id não deve continuar logando (ver deps.py:empresa_do_usuario,
    # que trata esse caso como "não vê nada", mas aqui evitamos até chegar nesse caso).
    c.execute(f"UPDATE usuarios SET ativo=0 WHERE papel='cliente' AND empresa_id IN ({emp})", params)
    c.execute(f"DELETE FROM bicos WHERE automacao_id IN "
              f"(SELECT id FROM automacoes WHERE empresa_id IN ({emp}))", params)
    c.execute(f"DELETE FROM automacoes WHERE empresa_id IN ({emp})", params)
    c.execute(f"DELETE FROM cameras WHERE empresa_id IN ({emp})", params)
    c.execute(f"DELETE FROM empresas WHERE {where_sql}", params)


def entidades_remover(id_: int) -> bool:
    with cursor() as c:
        _apagar_empresas(c, "entidade_id=?", (id_,))
        cur = c.execute("DELETE FROM entidades WHERE id=?", (id_,))
        return cur.rowcount > 0


def empresas_listar(entidade_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM empresas"
    params: list = []
    if entidade_id is not None:
        sql += " WHERE entidade_id=?"
        params.append(entidade_id)
    sql += " ORDER BY nome"
    with cursor() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def empresas_obter(id_: int) -> dict | None:
    with cursor() as c:
        r = c.execute("SELECT * FROM empresas WHERE id=?", (id_,)).fetchone()
        return dict(r) if r else None


def empresas_obter_por_cnpj(cnpj: str) -> dict | None:
    with cursor() as c:
        r = c.execute("SELECT * FROM empresas WHERE cnpj=?", (cnpj,)).fetchone()
        return dict(r) if r else None


def empresas_inserir(dados: dict) -> int:
    with cursor() as c:
        cur = c.execute(
            "INSERT INTO empresas (entidade_id, cnpj, nome, ativo, criado_em) VALUES (?,?,?,?,?)",
            (int(dados["entidade_id"]), dados["cnpj"], dados["nome"],
             1 if dados.get("ativo", True) else 0, _agora()),
        )
        return cur.lastrowid


def empresas_atualizar(id_: int, dados: dict) -> bool:
    # `api_key` e `retencao_dias_override` são geridos por endpoints próprios
    # (gerar/revogar chave, definir prazo) — este UPDATE nunca mexe neles, senão um
    # PUT comum de nome/CNPJ apagaria a chave/prazo sem intenção.
    with cursor() as c:
        cur = c.execute(
            "UPDATE empresas SET entidade_id=?, cnpj=?, nome=?, ativo=? WHERE id=?",
            (int(dados["entidade_id"]), dados["cnpj"], dados["nome"],
             1 if dados.get("ativo", True) else 0, id_),
        )
        return cur.rowcount > 0


def empresas_gerar_api_key(id_: int) -> str | None:
    """Gera (ou substitui) a api_key própria da empresa — opt-in: só quando ligada
    aqui essa empresa passa a exigir `X-API-Key`/`?api_key=` em `/api/leitura`."""
    import secrets
    chave = secrets.token_urlsafe(32)
    with cursor() as c:
        cur = c.execute("UPDATE empresas SET api_key=? WHERE id=?", (chave, id_))
        if cur.rowcount == 0:
            return None
    return chave


def empresas_revogar_api_key(id_: int) -> bool:
    """Volta a empresa para o padrão público (sem chave própria)."""
    with cursor() as c:
        cur = c.execute("UPDATE empresas SET api_key='' WHERE id=?", (id_,))
        return cur.rowcount > 0


def empresas_definir_retencao(id_: int, dias: int | None) -> bool:
    """`dias=None` volta a empresa a usar o `retencao_dias` global."""
    with cursor() as c:
        cur = c.execute("UPDATE empresas SET retencao_dias_override=? WHERE id=?", (dias, id_))
        return cur.rowcount > 0


def empresas_remover(id_: int) -> bool:
    with cursor() as c:
        existe = c.execute("SELECT 1 FROM empresas WHERE id=?", (id_,)).fetchone() is not None
        _apagar_empresas(c, "id=?", (id_,))
        return existe


def automacoes_listar(empresa_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM automacoes"
    params: list = []
    if empresa_id is not None:
        sql += " WHERE empresa_id=?"
        params.append(empresa_id)
    sql += " ORDER BY codigo"
    with cursor() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def automacoes_obter(id_: int) -> dict | None:
    with cursor() as c:
        r = c.execute("SELECT * FROM automacoes WHERE id=?", (id_,)).fetchone()
        return dict(r) if r else None


def _normalizar_codigo(codigo: str) -> str:
    """Tolera diferença de espaço/maiúscula no código vindo do roteador.

    O código não tem significado numérico — é só um rótulo opaco — então "1", " 1 " e
    "1 " são o mesmo bico/automação para qualquer humano. Como o lado que envia (roteador
    Java) é integração nova, é o tipo de diferença boba mais provável de acontecer.
    """
    return (codigo or "").strip().upper()


def automacoes_obter_por_codigo(empresa_id: int, codigo: str) -> dict | None:
    alvo = _normalizar_codigo(codigo)
    with cursor() as c:
        r = c.execute(
            "SELECT * FROM automacoes WHERE empresa_id=? AND UPPER(TRIM(codigo))=?",
            (empresa_id, alvo),
        ).fetchone()
        return dict(r) if r else None


def automacoes_inserir(dados: dict) -> int:
    with cursor() as c:
        cur = c.execute(
            "INSERT INTO automacoes (empresa_id, codigo, nome, ativo, criado_em) VALUES (?,?,?,?,?)",
            (int(dados["empresa_id"]), _normalizar_codigo(dados["codigo"]), dados.get("nome", ""),
             1 if dados.get("ativo", True) else 0, _agora()),
        )
        return cur.lastrowid


def automacoes_atualizar(id_: int, dados: dict) -> bool:
    with cursor() as c:
        cur = c.execute(
            "UPDATE automacoes SET empresa_id=?, codigo=?, nome=?, ativo=? WHERE id=?",
            (int(dados["empresa_id"]), _normalizar_codigo(dados["codigo"]), dados.get("nome", ""),
             1 if dados.get("ativo", True) else 0, id_),
        )
        return cur.rowcount > 0


def automacoes_remover(id_: int) -> bool:
    with cursor() as c:
        cur = c.execute("DELETE FROM automacoes WHERE id=?", (id_,))
        return cur.rowcount > 0


def bicos_listar(automacao_id: int | None = None, camera_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM bicos WHERE 1=1"
    params: list = []
    if automacao_id is not None:
        sql += " AND automacao_id=?"
        params.append(automacao_id)
    if camera_id is not None:
        # Casa os DOIS slots: um bico usa esta câmera tanto como principal quanto como
        # segunda. Quem consulta por câmera quer saber "quem depende dela" — o editor de
        # áreas (para listar os bicos a desenhar) e o guard que impede apagar câmera em
        # uso. Olhar só `camera_id` deixaria apagar uma câmera usada como secundária.
        sql += " AND (camera_id=? OR camera2_id=?)"
        params.extend([camera_id, camera_id])
    sql += " ORDER BY bomba, lado, codigo"
    with cursor() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def bicos_obter(id_: int) -> dict | None:
    with cursor() as c:
        r = c.execute("SELECT * FROM bicos WHERE id=?", (id_,)).fetchone()
        return dict(r) if r else None


def bicos_obter_por_codigo(automacao_id: int, codigo: str) -> dict | None:
    alvo = _normalizar_codigo(codigo)
    with cursor() as c:
        r = c.execute(
            "SELECT * FROM bicos WHERE automacao_id=? AND UPPER(TRIM(codigo))=?",
            (automacao_id, alvo),
        ).fetchone()
        return dict(r) if r else None


def _bico_bomba_lado(dados: dict) -> tuple[int | None, int | None]:
    bomba = dados.get("bomba")
    lado = dados.get("lado")
    return (int(bomba) if bomba not in (None, "") else None,
            int(lado) if lado not in (None, "") else None)


def _bico_camera2(dados: dict) -> int | None:
    """Segunda câmera é opcional: ausente, vazia ou nula significam "bico de uma câmera"."""
    valor = dados.get("camera2_id")
    return int(valor) if valor not in (None, "", 0) else None


def bicos_inserir(dados: dict) -> int:
    bomba, lado = _bico_bomba_lado(dados)
    with cursor() as c:
        cur = c.execute(
            """INSERT INTO bicos (automacao_id, codigo, nome, bomba, lado, camera_id,
                                  papel_camera, camera2_id, papel_camera2, ativo, criado_em)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (int(dados["automacao_id"]), _normalizar_codigo(dados["codigo"]), dados.get("nome", ""),
             bomba, lado, int(dados["camera_id"]),
             dados.get("papel_camera") or "traseira",
             _bico_camera2(dados), dados.get("papel_camera2") or "frente",
             1 if dados.get("ativo", True) else 0, _agora()),
        )
        return cur.lastrowid


def bicos_atualizar(id_: int, dados: dict) -> bool:
    bomba, lado = _bico_bomba_lado(dados)
    camera_id = int(dados["camera_id"])
    camera2_id = _bico_camera2(dados)
    # `roi`/`roi2` ficam fora do UPDATE (só os endpoints próprios escrevem neles) com UMA
    # exceção: trocar (ou remover) a câmera de um slot invalida o retângulo daquele slot,
    # que está em coordenadas do frame da câmera ANTIGA. Mantê-lo não dá erro nenhum — a
    # leitura passa a recortar o pedaço errado da imagem nova e o sintoma só aparece
    # depois, como queda na taxa de acerto daquele bico. Limpar aqui, na mesma transação,
    # é o que impede um cadastro editado de ficar silenciosamente pior do que estava.
    atual = bicos_obter(id_)
    limpar = []
    if atual is not None:
        if atual["camera_id"] != camera_id:
            limpar.append("roi=NULL")
        if atual.get("camera2_id") != camera2_id:
            limpar.append("roi2=NULL")
    with cursor() as c:
        cur = c.execute(
            f"""UPDATE bicos SET automacao_id=?, codigo=?, nome=?, bomba=?, lado=?, camera_id=?,
                                 papel_camera=?, camera2_id=?, papel_camera2=?,
                                 ativo=?{"".join(", " + s for s in limpar)}
               WHERE id=?""",
            (int(dados["automacao_id"]), _normalizar_codigo(dados["codigo"]), dados.get("nome", ""),
             bomba, lado, camera_id,
             dados.get("papel_camera") or "traseira",
             camera2_id, dados.get("papel_camera2") or "frente",
             1 if dados.get("ativo", True) else 0, id_),
        )
        return cur.rowcount > 0


def bicos_remover(id_: int) -> bool:
    with cursor() as c:
        cur = c.execute("DELETE FROM bicos WHERE id=?", (id_,))
        return cur.rowcount > 0


# Mapa fechado slot → coluna. O slot chega da requisição HTTP: interpolar o nome da
# coluna direto no SQL deixaria o cliente escolher onde escrever.
_COLUNA_ROI = {1: "roi", 2: "roi2"}


def bico_salvar_roi(id_: int, roi: dict, slot: int = 1) -> None:
    coluna = _COLUNA_ROI[slot]
    with cursor() as c:
        c.execute(f"UPDATE bicos SET {coluna}=? WHERE id=?", (json.dumps(roi), id_))


def bico_limpar_roi(id_: int, slot: int = 1) -> None:
    coluna = _COLUNA_ROI[slot]
    with cursor() as c:
        c.execute(f"UPDATE bicos SET {coluna}=NULL WHERE id=?", (id_,))


def cameras_do_bico(bico: dict) -> tuple[list[dict], list[str], str | None]:
    """Resolve as câmeras utilizáveis de um bico (1 ou 2), em ordem de slot.

    Devolve `(fontes, avisos, motivo_falha)`:

    - `fontes`: lista de `{"camera", "camera_id", "papel", "roi", "slot"}` — só as câmeras
      que existem E estão ativas. `camera` é a linha crua da tabela, do jeito que
      `EspecificacaoCamera.de_camera_db` consome.
    - `avisos`: texto por câmera descartada, para log e para a resposta do endpoint.
    - `motivo_falha`: None enquanto sobrar ao menos UMA câmera utilizável.

    DEGRADA em vez de falhar: um bico com duas câmeras que perde uma continua lendo pela
    outra. A segunda câmera existe para ser rede de segurança — se a ausência dela
    derrubasse a leitura, ela viraria um ponto de falha novo, o oposto do que motivou
    a feature. Para bico de UMA câmera nada muda: perder a única é falha total, com os
    mesmos motivos de sempre.

    Regra única de propósito: `resolver_bico` (fluxo do roteador) e `bico_verificar_ativo`
    (botão de teste do painel) chamam esta função em vez de repetir a checagem — foi
    justamente a duplicação dessa regra que deixou o botão de teste driblando o gate de
    `ativo` no passado.
    """
    slots = [(1, bico["camera_id"], bico.get("roi"), bico.get("papel_camera") or "traseira")]
    if bico.get("camera2_id"):
        slots.append((2, bico["camera2_id"], bico.get("roi2"),
                      bico.get("papel_camera2") or "frente"))

    fontes: list[dict] = []
    avisos: list[str] = []
    algum_inativo = False
    for slot, camera_id, roi, papel in slots:
        camera = cameras_obter(camera_id)
        if camera is None:
            avisos.append(f"câmera {camera_id} ({papel}) não existe mais no cadastro")
            continue
        if not camera["ativo"]:
            algum_inativo = True
            avisos.append(f"câmera '{camera['nome']}' ({papel}) está desativada no cadastro")
            continue
        fontes.append({"camera": camera, "camera_id": camera_id,
                       "papel": papel, "roi": roi, "slot": slot})

    if fontes:
        return fontes, avisos, None
    # Sem nenhuma câmera utilizável: "existe mas desativada" e "não existe" pedem
    # correções opostas (reativar vs. recadastrar), então continuam separadas. O motivo
    # "bico" para câmera inexistente espelha o comportamento anterior — um bico apontando
    # para câmera que sumiu é bico quebrado, não câmera desativada.
    return [], avisos, ("camera_inativa" if algum_inativo else "bico")


def resolver_bico(cnpj: str, automacao_codigo: str, bico_codigo: str) -> tuple[dict | None, str | None]:
    """Localiza a câmera+ROI de um bico a partir do GET reativo (cnpj/automacao/bico).

    3 buscas sequenciais (não um JOIN único) para que o motivo do erro aponte
    exatamente o nível de cadastro que falhou — pedido explícito da integração
    ("pode ser que tenha um cadastro do bico errado").

    Retorna (registro_mesclado, None) em caso de sucesso — registro combina campos
    da câmera (conexão) com os do bico (roi, ids) — ou (None, motivo) em caso de erro.
    `motivo` é "empresa"/"automacao"/"bico" quando o código não existe, ou
    "entidade_inativa"/"empresa_inativa"/"automacao_inativa"/"bico_inativo"/
    "camera_inativa" quando existe mas foi desativado no cadastro — distinção que
    importa porque a correção é diferente (cadastrar vs. reativar). Antes só o "ativo"
    do bico era checado: desativar um posto, automação, câmera ou a entidade dona do
    posto não impedia a leitura continuar respondendo por eles.
    """
    empresa = empresas_obter_por_cnpj(cnpj)
    if empresa is None:
        return None, "empresa"
    if not empresa["ativo"]:
        return None, "empresa_inativa"

    entidade = entidades_obter(empresa["entidade_id"])
    if entidade is not None and not entidade["ativo"]:
        return None, "entidade_inativa"

    automacao = automacoes_obter_por_codigo(empresa["id"], automacao_codigo)
    if automacao is None:
        return None, "automacao"
    if not automacao["ativo"]:
        return None, "automacao_inativa"

    bico = bicos_obter_por_codigo(automacao["id"], bico_codigo)
    if bico is None:
        return None, "bico"
    if not bico["ativo"]:
        return None, "bico_inativo"

    fontes, avisos, falha = cameras_do_bico(bico)
    if falha is not None:
        return None, falha

    # Os campos achatados (conexão da câmera na raiz, `camera_id`, `roi`) descrevem a
    # PRIMEIRA câmera utilizável — não necessariamente o slot 1. Assim quem lê o formato
    # antigo (`EspecificacaoCamera.de_camera_db(reg, cfg)`) recebe sempre uma câmera que
    # de fato funciona, mesmo quando a do slot 1 caiu. As duas câmeras completas ficam em
    # `reg["cameras"]`, que é o que o laço de leitura consome.
    principal = fontes[0]
    reg = {
        **principal["camera"],
        "camera_id": principal["camera_id"],
        "bico_id": bico["id"],
        "bico_codigo": bico["codigo"],
        "roi": principal["roi"],
        "cameras": fontes,
        "avisos": avisos,
        "automacao_id": automacao["id"],
        "empresa_id": empresa["id"],
        "entidade_id": empresa["entidade_id"],
        # Chave própria do cliente (opt-in) — vazia = /api/leitura continua público
        # para este posto. Ver app/web/leitura.py:leitura_reativa.
        "empresa_api_key": empresa["api_key"],
    }
    return reg, None


def bico_verificar_ativo(bico_id: int) -> tuple[dict | None, str | None]:
    """Mesmo gate de `resolver_bico()` (entidade/empresa/automação/bico/câmera ativos),
    mas partindo direto do id do bico — para endpoints internos (ex.: teste de leitura
    do editor de ROI) que já têm o id e não passariam pelo fluxo cnpj+automacao+bico do
    roteador. Sem isso, um bico/automação/câmera desativados ainda respondiam ao botão
    de teste do painel, driblando a mesma trava aplicada à leitura reativa de verdade.
    """
    bico = bicos_obter(bico_id)
    if bico is None:
        return None, "bico"
    if not bico["ativo"]:
        return None, "bico_inativo"

    automacao = automacoes_obter(bico["automacao_id"])
    if automacao is None:
        return None, "automacao"
    if not automacao["ativo"]:
        return None, "automacao_inativa"

    empresa = empresas_obter(automacao["empresa_id"])
    if empresa is None:
        return None, "empresa"
    if not empresa["ativo"]:
        return None, "empresa_inativa"

    entidade = entidades_obter(empresa["entidade_id"])
    if entidade is not None and not entidade["ativo"]:
        return None, "entidade_inativa"

    _fontes, _avisos, falha = cameras_do_bico(bico)
    if falha is not None:
        return None, falha

    return bico, None
