"""Detecções de placa, listas branca/negra e retenção de dados."""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone

from ._base import _agora

from ._base import cursor


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
) -> int:
    with cursor() as c:
        cur = c.execute(
            "INSERT INTO deteccoes (placa, padrao, confianca, snapshot, criado_em, camera_id, "
            "bbox, bico_id, frame, origem, camera_db_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (placa, padrao, confianca, snapshot, _agora(), camera_id,
             json.dumps(bbox) if bbox else None, bico_id, frame, origem, camera_db_id),
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
                        snapshot: str | None = None, frame: str | None = None) -> bool:
    """Atualiza placa/padrão/confiança de uma detecção existente — usado ao mesclar uma
    leitura nova com a detecção anterior do mesmo bico em vez de criar uma 2ª linha.

    Também renova `criado_em` para AGORA: a janela de cooldown deve deslizar a cada
    retry parecido, senão uma sequência de retries do roteador mais longa que um único
    cooldown_seg (ex.: 3 chamadas 70s uma da outra = 140s de ponta a ponta) volta a
    duplicar linha na 3ª chamada mesmo todas sendo o mesmo veículo.
    """
    with cursor() as c:
        cur = c.execute(
            "UPDATE deteccoes SET placa=?, padrao=?, confianca=?, criado_em=?, "
            "snapshot=COALESCE(?, snapshot), frame=COALESCE(?, frame) WHERE id=?",
            (placa, padrao, confianca, _agora(), snapshot, frame, id_),
        )
        return cur.rowcount > 0


def contar_deteccoes_placa(placa: str, incluir_testes: bool = False,
                            empresa_id: int | None = None) -> int:
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
    if not incluir_testes:
        sql += " AND COALESCE(d.origem, 'roteador') <> 'teste'"
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
) -> list[dict]:
    """Detecções com o posto/bico de origem resolvidos (LEFT JOIN — leituras antigas,
    anteriores ao multi-tenant, não têm bico e aparecem com os campos vazios).

    `placa_exata` troca o LIKE por igualdade — a busca da interface quer o LIKE
    (digitar parte da placa), a consulta de uma placa específica quer só ela.
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
    if not incluir_testes:
        sql += " AND COALESCE(d.origem, 'roteador') <> 'teste'"
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


def remover_deteccao(id_: int) -> bool:
    with cursor() as c:
        cur = c.execute("DELETE FROM deteccoes WHERE id=?", (id_,))
        return cur.rowcount > 0


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


def stats() -> dict:
    with cursor() as c:
        total = c.execute("SELECT COUNT(*) FROM deteccoes").fetchone()[0]
        hoje = c.execute(
            "SELECT COUNT(*) FROM deteccoes WHERE criado_em >= date('now')"
        ).fetchone()[0]
        top = [
            dict(r)
            for r in c.execute(
                "SELECT placa, COUNT(*) as ocorrencias FROM deteccoes "
                "GROUP BY placa ORDER BY ocorrencias DESC LIMIT 10"
            ).fetchall()
        ]
        return {"total": total, "hoje": hoje, "top": top}


def listas_listar(tipo: str | None = None) -> list[dict]:
    sql = "SELECT * FROM listas_placas"
    params: list = []
    if tipo:
        sql += " WHERE tipo=?"
        params.append(tipo)
    sql += " ORDER BY criado_em DESC"
    with cursor() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def listas_inserir(placa: str, tipo: str, descricao: str = "") -> int:
    with cursor() as c:
        cur = c.execute(
            "INSERT INTO listas_placas (placa, tipo, descricao, criado_em) VALUES (?, ?, ?, ?)",
            (placa, tipo, descricao, _agora()),
        )
        return cur.lastrowid


def listas_remover(id_: int) -> bool:
    with cursor() as c:
        cur = c.execute("DELETE FROM listas_placas WHERE id=?", (id_,))
        return cur.rowcount > 0


def listas_buscar(placa: str) -> dict | None:
    with cursor() as c:
        r = c.execute("SELECT * FROM listas_placas WHERE placa=?", (placa,)).fetchone()
        return dict(r) if r else None
