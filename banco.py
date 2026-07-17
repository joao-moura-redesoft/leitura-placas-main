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
    if "bico" in cols:
        c.execute("ALTER TABLE cameras RENAME COLUMN bico TO lado")
    if "roi" not in cols:
        c.execute("ALTER TABLE cameras ADD COLUMN roi TEXT")


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
            bomba INTEGER NOT NULL,
            lado INTEGER NOT NULL,
            camera_tipo TEXT NOT NULL DEFAULT 'intelbras',
            camera_indice TEXT NOT NULL DEFAULT '0',
            intelbras_host TEXT NOT NULL DEFAULT '',
            intelbras_porta TEXT NOT NULL DEFAULT '554',
            intelbras_usuario TEXT NOT NULL DEFAULT 'admin',
            intelbras_senha TEXT NOT NULL DEFAULT '',
            intelbras_canal TEXT NOT NULL DEFAULT '1',
            intelbras_subtype TEXT NOT NULL DEFAULT '1',
            intelbras_formato TEXT NOT NULL DEFAULT 'padrao',
            ativo INTEGER NOT NULL DEFAULT 1,
            criado_em TEXT NOT NULL,
            UNIQUE(bomba, lado)
        );
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
) -> int:
    with cursor() as c:
        cur = c.execute(
            "INSERT INTO deteccoes (placa, padrao, confianca, snapshot, criado_em, camera_id, bbox) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (placa, padrao, confianca, snapshot, _agora(), camera_id, json.dumps(bbox) if bbox else None),
        )
        return cur.lastrowid


def listar_deteccoes(
    placa: str | None = None,
    desde: str | None = None,
    ate: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    sql = "SELECT * FROM deteccoes WHERE 1=1"
    params: list = []
    if placa:
        sql += " AND placa LIKE ?"
        params.append(f"%{placa}%")
    if desde:
        sql += " AND criado_em >= ?"
        params.append(desde)
    if ate:
        sql += " AND criado_em <= ?"
        params.append(ate)
    sql += " ORDER BY criado_em DESC LIMIT ? OFFSET ?"
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

def cameras_listar() -> list[dict]:
    with cursor() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM cameras ORDER BY bomba, lado"
        ).fetchall()]


def cameras_obter(id_: int) -> dict | None:
    with cursor() as c:
        r = c.execute("SELECT * FROM cameras WHERE id=?", (id_,)).fetchone()
        return dict(r) if r else None


def cameras_inserir(dados: dict) -> int:
    with cursor() as c:
        cur = c.execute(
            """INSERT INTO cameras
               (nome, bomba, lado, camera_tipo, camera_indice,
                intelbras_host, intelbras_porta, intelbras_usuario, intelbras_senha,
                intelbras_canal, intelbras_subtype, intelbras_formato,
                rtsp_url_custom, ativo, criado_em)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                dados["nome"], int(dados["bomba"]), int(dados["lado"]),
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
               nome=?, bomba=?, lado=?, camera_tipo=?, camera_indice=?,
               intelbras_host=?, intelbras_porta=?, intelbras_usuario=?, intelbras_senha=?,
               intelbras_canal=?, intelbras_subtype=?, intelbras_formato=?,
               rtsp_url_custom=?, ativo=?
               WHERE id=?""",
            (
                dados["nome"], int(dados["bomba"]), int(dados["lado"]),
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


def camera_salvar_roi(id_: int, roi: dict) -> None:
    with cursor() as c:
        c.execute("UPDATE cameras SET roi=? WHERE id=?", (json.dumps(roi), id_))


def camera_limpar_roi(id_: int) -> None:
    with cursor() as c:
        c.execute("UPDATE cameras SET roi=NULL WHERE id=?", (id_,))


def cameras_remover(id_: int) -> bool:
    with cursor() as c:
        cur = c.execute("DELETE FROM cameras WHERE id=?", (id_,))
        return cur.rowcount > 0


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
