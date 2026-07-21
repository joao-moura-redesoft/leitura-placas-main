"""Camada SQLite — deteccoes e listas_placas."""
from __future__ import annotations
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("placas.db")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


@contextmanager
def cursor():
    c = _conn()
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _migrar(c: sqlite3.Connection) -> None:
    """Aplica migrações incrementais de schema."""
    cols = {row[1] for row in c.execute("PRAGMA table_info(cameras)").fetchall()}
    if "rtsp_url_custom" not in cols:
        c.execute("ALTER TABLE cameras ADD COLUMN rtsp_url_custom TEXT NOT NULL DEFAULT ''")

    # `cameras` virou registro puro de CONEXÃO — bomba/lado/roi passaram para `bicos`.
    # SQLite não remove coluna nem constraint via ALTER, e o CREATE TABLE IF NOT EXISTS
    # não altera uma tabela que já existe: sem reconstruir aqui, um banco antigo mantém
    # `bomba NOT NULL` e todo INSERT de câmera quebra com IntegrityError.
    if {"bomba", "lado", "roi"} & cols:
        c.executescript("""
        CREATE TABLE cameras_novo (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            empresa_id INTEGER REFERENCES empresas(id) ON DELETE CASCADE,
            local TEXT NOT NULL DEFAULT '',
            camera_tipo TEXT NOT NULL DEFAULT 'intelbras',
            camera_indice TEXT NOT NULL DEFAULT '0',
            intelbras_host TEXT NOT NULL DEFAULT '',
            intelbras_porta TEXT NOT NULL DEFAULT '554',
            intelbras_usuario TEXT NOT NULL DEFAULT 'admin',
            intelbras_senha TEXT NOT NULL DEFAULT '',
            intelbras_canal TEXT NOT NULL DEFAULT '1',
            intelbras_subtype TEXT NOT NULL DEFAULT '1',
            intelbras_formato TEXT NOT NULL DEFAULT 'padrao',
            rtsp_url_custom TEXT NOT NULL DEFAULT '',
            ativo INTEGER NOT NULL DEFAULT 1,
            criado_em TEXT NOT NULL
        );
        INSERT INTO cameras_novo
            (id, nome, camera_tipo, camera_indice, intelbras_host, intelbras_porta,
             intelbras_usuario, intelbras_senha, intelbras_canal, intelbras_subtype,
             intelbras_formato, rtsp_url_custom, ativo, criado_em)
        SELECT id, nome, camera_tipo, camera_indice, intelbras_host, intelbras_porta,
               intelbras_usuario, intelbras_senha, intelbras_canal, intelbras_subtype,
               intelbras_formato, rtsp_url_custom, ativo, criado_em
        FROM cameras;
        DROP TABLE cameras;
        ALTER TABLE cameras_novo RENAME TO cameras;
        """)
        cols = {row[1] for row in c.execute("PRAGMA table_info(cameras)").fetchall()}

    # Câmera pertence a um posto (empresa) e diz onde está fisicamente instalada.
    # `empresa_id` fica nullable no schema porque bancos anteriores têm câmeras sem dono
    # — a API/UI exigem o vínculo, e as órfãs aparecem como "sem empresa" para atribuição.
    if "empresa_id" not in cols:
        c.execute("ALTER TABLE cameras ADD COLUMN empresa_id INTEGER REFERENCES empresas(id) ON DELETE CASCADE")
    if "local" not in cols:
        c.execute("ALTER TABLE cameras ADD COLUMN local TEXT NOT NULL DEFAULT ''")

    cols_det = {row[1] for row in c.execute("PRAGMA table_info(deteccoes)").fetchall()}
    if "bico_id" not in cols_det:
        c.execute("ALTER TABLE deteccoes ADD COLUMN bico_id INTEGER")
    # Quadro inteiro (com a marcação de onde a placa foi achada). O `snapshot` guarda só
    # o recorte da placa; sem o quadro não dá para conferir o contexto depois — se pegou
    # o carro certo, se a área do bico estava bem posicionada.
    if "frame" not in cols_det:
        c.execute("ALTER TABLE deteccoes ADD COLUMN frame TEXT")
    # De onde veio a leitura: 'roteador' (produção), 'teste' (botão da interface) ou
    # 'pipeline' (detecção contínua). Sem isso um teste manual fica indistinguível de
    # um abastecimento real e contamina o histórico e as estatísticas.
    if "origem" not in cols_det:
        c.execute("ALTER TABLE deteccoes ADD COLUMN origem TEXT NOT NULL DEFAULT 'roteador'")


def inicializar() -> None:
    with cursor() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS deteccoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            placa TEXT NOT NULL,
            padrao TEXT NOT NULL,
            confianca REAL NOT NULL,
            snapshot TEXT,
            criado_em TEXT NOT NULL,
            camera_id TEXT,
            bbox TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_deteccoes_placa ON deteccoes(placa);
        CREATE INDEX IF NOT EXISTS idx_deteccoes_criado ON deteccoes(criado_em DESC);

        CREATE TABLE IF NOT EXISTS listas_placas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            placa TEXT NOT NULL UNIQUE,
            tipo TEXT NOT NULL CHECK(tipo IN ('branca','negra')),
            descricao TEXT,
            criado_em TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS usuarios (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            nome      TEXT    NOT NULL,
            email     TEXT    UNIQUE NOT NULL,
            senha     TEXT    NOT NULL,
            papel     TEXT    NOT NULL DEFAULT 'admin',
            ativo     INTEGER NOT NULL DEFAULT 1,
            criado_em TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cameras (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            empresa_id INTEGER REFERENCES empresas(id) ON DELETE CASCADE,
            local TEXT NOT NULL DEFAULT '',
            camera_tipo TEXT NOT NULL DEFAULT 'intelbras',
            camera_indice TEXT NOT NULL DEFAULT '0',
            intelbras_host TEXT NOT NULL DEFAULT '',
            intelbras_porta TEXT NOT NULL DEFAULT '554',
            intelbras_usuario TEXT NOT NULL DEFAULT 'admin',
            intelbras_senha TEXT NOT NULL DEFAULT '',
            intelbras_canal TEXT NOT NULL DEFAULT '1',
            intelbras_subtype TEXT NOT NULL DEFAULT '1',
            intelbras_formato TEXT NOT NULL DEFAULT 'padrao',
            rtsp_url_custom TEXT NOT NULL DEFAULT '',
            ativo INTEGER NOT NULL DEFAULT 1,
            criado_em TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS entidades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            ativo INTEGER NOT NULL DEFAULT 1,
            criado_em TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS empresas (            -- CNPJ = 1 posto físico (1:1)
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entidade_id INTEGER NOT NULL REFERENCES entidades(id) ON DELETE CASCADE,
            cnpj TEXT NOT NULL UNIQUE,
            nome TEXT NOT NULL,
            ativo INTEGER NOT NULL DEFAULT 1,
            criado_em TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_empresas_entidade ON empresas(entidade_id);

        CREATE TABLE IF NOT EXISTS automacoes (          -- até 2 por posto
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            empresa_id INTEGER NOT NULL REFERENCES empresas(id) ON DELETE CASCADE,
            codigo TEXT NOT NULL,
            nome TEXT NOT NULL DEFAULT '',
            ativo INTEGER NOT NULL DEFAULT 1,
            criado_em TEXT NOT NULL,
            UNIQUE(empresa_id, codigo)
        );
        CREATE INDEX IF NOT EXISTS idx_automacoes_empresa ON automacoes(empresa_id);

        CREATE TABLE IF NOT EXISTS bicos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            automacao_id INTEGER NOT NULL REFERENCES automacoes(id) ON DELETE CASCADE,
            codigo TEXT NOT NULL,                          -- chave usada na resolução do GET
            nome TEXT NOT NULL DEFAULT '',
            bomba INTEGER,                                 -- opcional, só organização/UI
            lado INTEGER,                                  -- opcional, só organização/UI
            camera_id INTEGER NOT NULL REFERENCES cameras(id) ON DELETE RESTRICT,
            roi TEXT,                                      -- {x,y,w,h} — área própria deste bico
            ativo INTEGER NOT NULL DEFAULT 1,
            criado_em TEXT NOT NULL,
            UNIQUE(automacao_id, codigo)
        );
        CREATE INDEX IF NOT EXISTS idx_bicos_automacao ON bicos(automacao_id);
        CREATE INDEX IF NOT EXISTS idx_bicos_camera ON bicos(camera_id);

        -- Log de TODA chamada do roteador ao endpoint reativo, inclusive as recusadas.
        -- É o que dá visibilidade da integração: sem isso, um cadastro errado do lado do
        -- posto só aparece no log do servidor e ninguém percebe.
        CREATE TABLE IF NOT EXISTS chamadas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            criado_em TEXT NOT NULL,
            entidade TEXT NOT NULL DEFAULT '',
            cnpj TEXT NOT NULL DEFAULT '',
            automacao TEXT NOT NULL DEFAULT '',
            bico TEXT NOT NULL DEFAULT '',
            bico_id INTEGER,
            empresa_id INTEGER,
            status TEXT NOT NULL,        -- ok | sem_placa | erro_cadastro | erro_camera
            motivo TEXT NOT NULL DEFAULT '',
            placa TEXT,
            acordo REAL,
            tentativas INTEGER,
            duracao_ms INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_chamadas_criado ON chamadas(criado_em DESC);
        CREATE INDEX IF NOT EXISTS idx_chamadas_empresa ON chamadas(empresa_id);
        CREATE INDEX IF NOT EXISTS idx_chamadas_status ON chamadas(status);
        """)
        _migrar(c)


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat()


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
) -> int:
    with cursor() as c:
        cur = c.execute(
            "INSERT INTO deteccoes (placa, padrao, confianca, snapshot, criado_em, camera_id, "
            "bbox, bico_id, frame, origem) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (placa, padrao, confianca, snapshot, _agora(), camera_id,
             json.dumps(bbox) if bbox else None, bico_id, frame, origem),
        )
        return cur.lastrowid


def listar_deteccoes(
    placa: str | None = None,
    desde: str | None = None,
    ate: str | None = None,
    limit: int = 50,
    offset: int = 0,
    empresa_id: int | None = None,
    bico_id: int | None = None,
    incluir_testes: bool = False,
) -> list[dict]:
    """Detecções com o posto/bico de origem resolvidos (LEFT JOIN — leituras antigas,
    anteriores ao multi-tenant, não têm bico e aparecem com os campos vazios)."""
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
    sql += " ORDER BY d.criado_em DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with cursor() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def remover_deteccao(id_: int) -> bool:
    with cursor() as c:
        cur = c.execute("DELETE FROM deteccoes WHERE id=?", (id_,))
        return cur.rowcount > 0


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
    with cursor() as c:
        cur = c.execute(
            "UPDATE empresas SET entidade_id=?, cnpj=?, nome=?, ativo=? WHERE id=?",
            (int(dados["entidade_id"]), dados["cnpj"], dados["nome"],
             1 if dados.get("ativo", True) else 0, id_),
        )
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


def automacoes_obter_por_codigo(empresa_id: int, codigo: str) -> dict | None:
    with cursor() as c:
        r = c.execute(
            "SELECT * FROM automacoes WHERE empresa_id=? AND codigo=?", (empresa_id, codigo)
        ).fetchone()
        return dict(r) if r else None


def automacoes_inserir(dados: dict) -> int:
    with cursor() as c:
        cur = c.execute(
            "INSERT INTO automacoes (empresa_id, codigo, nome, ativo, criado_em) VALUES (?,?,?,?,?)",
            (int(dados["empresa_id"]), dados["codigo"], dados.get("nome", ""),
             1 if dados.get("ativo", True) else 0, _agora()),
        )
        return cur.lastrowid


def automacoes_atualizar(id_: int, dados: dict) -> bool:
    with cursor() as c:
        cur = c.execute(
            "UPDATE automacoes SET empresa_id=?, codigo=?, nome=?, ativo=? WHERE id=?",
            (int(dados["empresa_id"]), dados["codigo"], dados.get("nome", ""),
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
        sql += " AND camera_id=?"
        params.append(camera_id)
    sql += " ORDER BY bomba, lado, codigo"
    with cursor() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def bicos_obter(id_: int) -> dict | None:
    with cursor() as c:
        r = c.execute("SELECT * FROM bicos WHERE id=?", (id_,)).fetchone()
        return dict(r) if r else None


def bicos_obter_por_codigo(automacao_id: int, codigo: str) -> dict | None:
    with cursor() as c:
        r = c.execute(
            "SELECT * FROM bicos WHERE automacao_id=? AND codigo=?", (automacao_id, codigo)
        ).fetchone()
        return dict(r) if r else None


def _bico_bomba_lado(dados: dict) -> tuple[int | None, int | None]:
    bomba = dados.get("bomba")
    lado = dados.get("lado")
    return (int(bomba) if bomba not in (None, "") else None,
            int(lado) if lado not in (None, "") else None)


def bicos_inserir(dados: dict) -> int:
    bomba, lado = _bico_bomba_lado(dados)
    with cursor() as c:
        cur = c.execute(
            """INSERT INTO bicos (automacao_id, codigo, nome, bomba, lado, camera_id, ativo, criado_em)
               VALUES (?,?,?,?,?,?,?,?)""",
            (int(dados["automacao_id"]), dados["codigo"], dados.get("nome", ""),
             bomba, lado, int(dados["camera_id"]),
             1 if dados.get("ativo", True) else 0, _agora()),
        )
        return cur.lastrowid


def bicos_atualizar(id_: int, dados: dict) -> bool:
    bomba, lado = _bico_bomba_lado(dados)
    with cursor() as c:
        cur = c.execute(
            """UPDATE bicos SET automacao_id=?, codigo=?, nome=?, bomba=?, lado=?, camera_id=?, ativo=?
               WHERE id=?""",
            (int(dados["automacao_id"]), dados["codigo"], dados.get("nome", ""),
             bomba, lado, int(dados["camera_id"]),
             1 if dados.get("ativo", True) else 0, id_),
        )
        return cur.rowcount > 0


def bicos_remover(id_: int) -> bool:
    with cursor() as c:
        cur = c.execute("DELETE FROM bicos WHERE id=?", (id_,))
        return cur.rowcount > 0


def bico_salvar_roi(id_: int, roi: dict) -> None:
    with cursor() as c:
        c.execute("UPDATE bicos SET roi=? WHERE id=?", (json.dumps(roi), id_))


def bico_limpar_roi(id_: int) -> None:
    with cursor() as c:
        c.execute("UPDATE bicos SET roi=NULL WHERE id=?", (id_,))


def resolver_bico(cnpj: str, automacao_codigo: str, bico_codigo: str) -> tuple[dict | None, str | None]:
    """Localiza a câmera+ROI de um bico a partir do GET reativo (cnpj/automacao/bico).

    3 buscas sequenciais (não um JOIN único) para que o motivo do erro aponte
    exatamente o nível de cadastro que falhou — pedido explícito da integração
    ("pode ser que tenha um cadastro do bico errado").

    Retorna (registro_mesclado, None) em caso de sucesso — registro combina campos
    da câmera (conexão) com os do bico (roi, ids) — ou (None, motivo) em caso de erro,
    motivo em {"empresa", "automacao", "bico"}.
    """
    empresa = empresas_obter_por_cnpj(cnpj)
    if empresa is None:
        return None, "empresa"

    automacao = automacoes_obter_por_codigo(empresa["id"], automacao_codigo)
    if automacao is None:
        return None, "automacao"

    bico = bicos_obter_por_codigo(automacao["id"], bico_codigo)
    if bico is None or not bico["ativo"]:
        return None, "bico"

    camera = cameras_obter(bico["camera_id"])
    if camera is None:
        return None, "bico"

    reg = {
        **camera,
        "camera_id": bico["camera_id"],
        "bico_id": bico["id"],
        "bico_codigo": bico["codigo"],
        "roi": bico["roi"],
        "automacao_id": automacao["id"],
        "empresa_id": empresa["id"],
        "entidade_id": empresa["entidade_id"],
    }
    return reg, None


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


def chamadas_resumo(horas: int = 24) -> dict:
    """Agregados da integração para o dashboard: volume, taxa de acerto, onde está falhando."""
    desde = f"-{int(horas)} hours"
    with cursor() as c:
        por_status = {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) n FROM chamadas WHERE criado_em >= datetime('now', ?) "
            "GROUP BY status", (desde,))}
        total = sum(por_status.values())
        acordo = c.execute(
            "SELECT AVG(acordo) a FROM chamadas WHERE status='ok' AND acordo IS NOT NULL "
            "AND criado_em >= datetime('now', ?)", (desde,)).fetchone()["a"]
        duracao = c.execute(
            "SELECT AVG(duracao_ms) d FROM chamadas WHERE duracao_ms IS NOT NULL "
            "AND criado_em >= datetime('now', ?)", (desde,)).fetchone()["d"]
        por_posto = [dict(r) for r in c.execute(
            "SELECT COALESCE(em.nome, ch.cnpj) AS posto, "
            "  SUM(CASE WHEN ch.status='ok' THEN 1 ELSE 0 END) AS ok, "
            "  COUNT(*) AS total "
            "FROM chamadas ch LEFT JOIN empresas em ON ch.empresa_id = em.id "
            "WHERE ch.criado_em >= datetime('now', ?) "
            "GROUP BY posto ORDER BY total DESC LIMIT 10", (desde,))]
        # Onde o cadastro está divergindo do que o roteador envia
        motivos = [dict(r) for r in c.execute(
            "SELECT motivo, COUNT(*) n FROM chamadas "
            "WHERE status IN ('erro_cadastro','erro_camera') AND criado_em >= datetime('now', ?) "
            "GROUP BY motivo ORDER BY n DESC LIMIT 8", (desde,))]
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


# ─── Usuários ──────────────────────────────────────────────────────────────

def contar_usuarios() -> int:
    with cursor() as c:
        return c.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]


def criar_usuario(nome: str, email: str, senha_hash: str, papel: str = "admin", ativo: int = 1) -> int | None:
    try:
        with cursor() as c:
            cur = c.execute(
                "INSERT INTO usuarios (nome, email, senha, papel, ativo, criado_em) VALUES (?,?,?,?,?,?)",
                (nome, email, senha_hash, papel, ativo, _agora()),
            )
            return cur.lastrowid
    except Exception:
        return None


def buscar_usuario_email(email: str) -> dict | None:
    with cursor() as c:
        r = c.execute("SELECT * FROM usuarios WHERE email=?", (email.strip().lower(),)).fetchone()
        return dict(r) if r else None


def buscar_usuario_id(id_: int) -> dict | None:
    with cursor() as c:
        r = c.execute("SELECT * FROM usuarios WHERE id=?", (id_,)).fetchone()
        return dict(r) if r else None
