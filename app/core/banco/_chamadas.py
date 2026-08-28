"""Log das chamadas do roteador ao endpoint reativo e seus agregados."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from ._base import _agora

from ._base import cursor


# ─── Chamadas do roteador (leitura reativa) ────────────────────────────────

# Colunas NOT NULL sem valor natural quando o chamador omite: o default mora aqui porque
# um `.get(k)` cru devolveria None e o INSERT quebraria. Tem de casar com o DEFAULT
# declarado na tabela (app/core/banco/_esquema.py).
_PADRAO_CAMPO = {"modo": "completo"}


def registrar_chamada(**dados) -> int:
    campos = ("entidade", "cnpj", "automacao", "bico", "bico_id", "empresa_id",
              "status", "motivo", "placa", "acordo", "tentativas", "duracao_ms", "modo")
    # Resolvido por NOME, nunca por posição: a versão anterior montava a lista como
    # `[...for k in campos[:-1]] + [modo]`, o que só funciona enquanto `modo` for o ÚLTIMO
    # campo. Acrescentar qualquer coluna depois dele desalinharia valores e colunas em
    # silêncio — o INSERT continua válido, e os dados é que saem trocados.
    valores = []
    for k in campos:
        v = dados.get(k)
        if v in (None, "") and k in _PADRAO_CAMPO:
            v = _PADRAO_CAMPO[k]
        valores.append(v)
    with cursor() as c:
        cur = c.execute(
            f"INSERT INTO chamadas (criado_em, {', '.join(campos)}) "
            f"VALUES (?, {', '.join('?' * len(campos))})",
            (_agora(), *valores),
        )
        return cur.lastrowid


def chamadas_listar(limit: int = 50, empresa_id: int | None = None,
                    status: str | None = None, apenas_erros: bool = False) -> list[dict]:
    sql = ("SELECT ch.*, em.nome AS empresa_nome FROM chamadas ch "
           "LEFT JOIN empresas em ON ch.empresa_id = em.id WHERE 1=1")
    params: list = []
    if empresa_id is not None:
        sql += " AND ch.empresa_id = ?"
        params.append(empresa_id)
    if status:
        sql += " AND ch.status = ?"
        params.append(status)
    if apenas_erros:
        sql += " AND ch.status IN ('erro_cadastro','erro_camera')"
    sql += " ORDER BY ch.criado_em DESC LIMIT ?"
    params.append(limit)
    with cursor() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def chamadas_resumo(horas: int = 24, empresa_id: int | None = None) -> dict:
    """Agregados da integração para o dashboard: volume, taxa de acerto, onde está falhando.

    `empresa_id`: escopa tudo a um único posto — usado quando quem pede é um usuário
    'cliente' (app/web/deps.py:empresa_do_usuario), que só pode ver o próprio posto.
    """
    # Corte em ISO-8601, calculado em Python — NÃO `?` do SQLite.
    #
    # `criado_em` é gravado por `_base._agora()` = `datetime.now(timezone.utc).isoformat()`,
    # que usa 'T' como separador ('2026-08-27T13:38:28+00:00'). `datetime('now', ...)`
    # devolve com ESPAÇO ('2026-08-26 13:42:26'). A comparação é textual e trava na posição
    # 10, onde 'T' (0x54) > ' ' (0x20) — então a hora do corte nunca era avaliada e o filtro
    # de 24 h degenerava em filtro de DATA. Medido: uma linha de 25 h e outra de 37 h atrás
    # PASSAVAM num filtro de 24 h. A janela real oscilava entre 24 h e 48 h conforme a hora
    # do dia, e "última 1 hora" virava "hoje inteiro mais ontem inteiro".
    # (Auditoria 27/08/2026, achado A1.)
    desde = (datetime.now(timezone.utc) - timedelta(hours=int(horas))).isoformat()
    filtro_emp = " AND empresa_id = ?" if empresa_id is not None else ""
    params_emp: tuple = (empresa_id,) if empresa_id is not None else ()
    with cursor() as c:
        por_status = {r["status"]: r["n"] for r in c.execute(
            f"SELECT status, COUNT(*) n FROM chamadas WHERE criado_em >= ?{filtro_emp} "
            "GROUP BY status", (desde, *params_emp))}
        total = sum(por_status.values())
        # Média sobre TODA chamada que produziu placa, confirmada ou não: restringir a
        # 'ok' esconderia as leituras fracas justamente da métrica que serve para detectar
        # que a qualidade caiu (câmera suja, ângulo ruim, moto). Uma query só para as duas
        # médias (cada `CASE` aplica o filtro que antes era um WHERE próprio) — elas
        # compartilham tabela e período, só o filtro interno muda.
        medias = c.execute(
            f"SELECT "
            f"  AVG(CASE WHEN status IN ('ok','nao_confirmada') AND acordo IS NOT NULL "
            f"           THEN acordo END) AS a, "
            f"  AVG(CASE WHEN duracao_ms IS NOT NULL THEN duracao_ms END) AS d "
            f"FROM chamadas WHERE criado_em >= ?{filtro_emp}",
            (desde, *params_emp)).fetchone()
        acordo, duracao = medias["a"], medias["d"]
        filtro_emp_ch = " AND ch.empresa_id = ?" if empresa_id is not None else ""
        por_posto = [dict(r) for r in c.execute(
            "SELECT COALESCE(em.nome, ch.cnpj) AS posto, "
            "  SUM(CASE WHEN ch.status='ok' THEN 1 ELSE 0 END) AS ok, "
            "  COUNT(*) AS total "
            "FROM chamadas ch LEFT JOIN empresas em ON ch.empresa_id = em.id "
            f"WHERE ch.criado_em >= ?{filtro_emp_ch} "
            "GROUP BY posto ORDER BY total DESC LIMIT 10", (desde, *params_emp))]
        # Onde o cadastro está divergindo do que o roteador envia
        motivos = [dict(r) for r in c.execute(
            f"SELECT motivo, COUNT(*) n FROM chamadas "
            f"WHERE status IN ('erro_cadastro','erro_camera') AND criado_em >= ?{filtro_emp} "
            "GROUP BY motivo ORDER BY n DESC LIMIT 8", (desde, *params_emp))]
        # Quebra por perfil de leitura. O modo rápido tem, POR DESENHO, taxa de sucesso
        # menor e duração muito menor — somados num número só, o painel mostraria uma
        # queda de qualidade que ninguém causou, e a investigação iria parar nas câmeras.
        # `taxa_sucesso` global continua existindo (contrato do painel), mas agora ao lado
        # do detalhe que a explica.
        por_modo = [dict(r) for r in c.execute(
            "SELECT modo, COUNT(*) AS total, "
            "  SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok, "
            "  CAST(AVG(duracao_ms) AS INTEGER) AS duracao_media_ms "
            f"FROM chamadas WHERE criado_em >= ?{filtro_emp} "
            "GROUP BY modo ORDER BY total DESC", (desde, *params_emp))]
    for m in por_modo:
        m["taxa_sucesso"] = round(m["ok"] / m["total"], 3) if m["total"] else None
    ok = por_status.get("ok", 0)
    return {
        "horas": horas,
        "total": total,
        "ok": ok,
        "sem_placa": por_status.get("sem_placa", 0),
        "nao_confirmada": por_status.get("nao_confirmada", 0),
        "erro_cadastro": por_status.get("erro_cadastro", 0),
        "erro_camera": por_status.get("erro_camera", 0),
        "taxa_sucesso": round(ok / total, 3) if total else None,
        "acordo_medio": round(acordo, 3) if acordo is not None else None,
        "duracao_media_ms": int(duracao) if duracao is not None else None,
        "por_posto": por_posto,
        "por_modo": por_modo,
        "motivos": motivos,
    }
