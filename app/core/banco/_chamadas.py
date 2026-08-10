"""Log das chamadas do roteador ao endpoint reativo e seus agregados."""
from __future__ import annotations
from ._base import _agora

from ._base import cursor


# ─── Chamadas do roteador (leitura reativa) ────────────────────────────────

def registrar_chamada(**dados) -> int:
    campos = ("entidade", "cnpj", "automacao", "bico", "bico_id", "empresa_id",
              "status", "motivo", "placa", "acordo", "tentativas", "duracao_ms")
    valores = [dados.get(k) for k in campos]
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
    desde = f"-{int(horas)} hours"
    filtro_emp = " AND empresa_id = ?" if empresa_id is not None else ""
    params_emp: tuple = (empresa_id,) if empresa_id is not None else ()
    with cursor() as c:
        por_status = {r["status"]: r["n"] for r in c.execute(
            f"SELECT status, COUNT(*) n FROM chamadas WHERE criado_em >= datetime('now', ?){filtro_emp} "
            "GROUP BY status", (desde, *params_emp))}
        total = sum(por_status.values())
        acordo = c.execute(
            f"SELECT AVG(acordo) a FROM chamadas WHERE status='ok' AND acordo IS NOT NULL "
            f"AND criado_em >= datetime('now', ?){filtro_emp}", (desde, *params_emp)).fetchone()["a"]
        duracao = c.execute(
            f"SELECT AVG(duracao_ms) d FROM chamadas WHERE duracao_ms IS NOT NULL "
            f"AND criado_em >= datetime('now', ?){filtro_emp}", (desde, *params_emp)).fetchone()["d"]
        filtro_emp_ch = " AND ch.empresa_id = ?" if empresa_id is not None else ""
        por_posto = [dict(r) for r in c.execute(
            "SELECT COALESCE(em.nome, ch.cnpj) AS posto, "
            "  SUM(CASE WHEN ch.status='ok' THEN 1 ELSE 0 END) AS ok, "
            "  COUNT(*) AS total "
            "FROM chamadas ch LEFT JOIN empresas em ON ch.empresa_id = em.id "
            f"WHERE ch.criado_em >= datetime('now', ?){filtro_emp_ch} "
            "GROUP BY posto ORDER BY total DESC LIMIT 10", (desde, *params_emp))]
        # Onde o cadastro está divergindo do que o roteador envia
        motivos = [dict(r) for r in c.execute(
            f"SELECT motivo, COUNT(*) n FROM chamadas "
            f"WHERE status IN ('erro_cadastro','erro_camera') AND criado_em >= datetime('now', ?){filtro_emp} "
            "GROUP BY motivo ORDER BY n DESC LIMIT 8", (desde, *params_emp))]
    ok = por_status.get("ok", 0)
    return {
        "horas": horas,
        "total": total,
        "ok": ok,
        "sem_placa": por_status.get("sem_placa", 0),
        "erro_cadastro": por_status.get("erro_cadastro", 0),
        "erro_camera": por_status.get("erro_camera", 0),
        "taxa_sucesso": round(ok / total, 3) if total else None,
        "acordo_medio": round(acordo, 3) if acordo is not None else None,
        "duracao_media_ms": int(duracao) if duracao is not None else None,
        "por_posto": por_posto,
        "motivos": motivos,
    }
