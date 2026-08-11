"""Criação das tabelas e migrações incrementais de schema."""
from __future__ import annotations
import sqlite3
from pathlib import Path

from ._base import caminho

from ._base import cursor


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
    # `camera_id` (acima) guarda só o TIPO da câmera ("usb"/"rtsp"), não o ID real — não dá
    # pra saber DE QUAL câmera veio uma detecção quando há mais de uma do mesmo tipo. Sem
    # isso, não tem como cruzar uma detecção 'pipeline' (que nunca tem bico_id) com uma
    # 'roteador'/'teste' da mesma câmera física para evitar duplicar o mesmo veículo.
    if "camera_db_id" not in cols_det:
        c.execute("ALTER TABLE deteccoes ADD COLUMN camera_db_id INTEGER")
    # `deteccoes` é alimentada por toda leitura reativa de todos os postos — sem
    # índice, listar/filtrar por bico vira table scan conforme a tabela cresce.
    # Fica em `_migrar` (não no CREATE TABLE inicial) porque só depois daqui a
    # coluna `bico_id` está garantidamente presente, inclusive em bancos antigos.
    c.execute("CREATE INDEX IF NOT EXISTS idx_deteccoes_bico ON deteccoes(bico_id)")

    # Chave de API própria por cliente (opt-in): vazia = /api/leitura continua público
    # para esse posto (comportamento de sempre); preenchida = passa a exigir a chave
    # nas chamadas daquele CNPJ. Não é obrigatório para ninguém só por existir a coluna.
    cols_emp = {row[1] for row in c.execute("PRAGMA table_info(empresas)").fetchall()}
    if "api_key" not in cols_emp:
        c.execute("ALTER TABLE empresas ADD COLUMN api_key TEXT NOT NULL DEFAULT ''")
    # Prazo de retenção próprio (LGPD por cliente): NULL = usa o `retencao_dias` global.
    if "retencao_dias_override" not in cols_emp:
        c.execute("ALTER TABLE empresas ADD COLUMN retencao_dias_override INTEGER")

    # Usuário do painel restrito a UMA empresa ("cliente"): NULL = admin, vê tudo (papel
    # continua sendo o que manda — isto só faz sentido quando papel='cliente').
    cols_usr = {row[1] for row in c.execute("PRAGMA table_info(usuarios)").fetchall()}
    if "empresa_id" not in cols_usr:
        c.execute("ALTER TABLE usuarios ADD COLUMN empresa_id INTEGER REFERENCES empresas(id) ON DELETE SET NULL")
    # Último login bem-sucedido — mostrado na lista de usuários pra achar conta
    # esquecida/nunca usada (cliente que nunca entrou, admin dormente).
    if "ultimo_login" not in cols_usr:
        c.execute("ALTER TABLE usuarios ADD COLUMN ultimo_login TEXT")


def inicializar() -> None:
    # Cria o diretório do banco quando o caminho aponta para uma subpasta (container:
    # /app/dados/placas.db). Sem isso o sqlite3.connect falha com "unable to open
    # database file" numa mensagem que não deixa claro que o problema é a pasta.
    destino = caminho()
    if destino.parent != Path("."):
        destino.parent.mkdir(parents=True, exist_ok=True)
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

        -- Sessões de login. Ficavam só em memória: todo restart deslogava todo mundo
        -- (mesmo com o cookie ainda válido por 7 dias no navegador) e era impossível
        -- rodar mais de um worker uvicorn, porque cada processo teria seu próprio dicionário.
        -- ON DELETE CASCADE: remover o usuário derruba as sessões dele junto.
        CREATE TABLE IF NOT EXISTS sessoes (
            token     TEXT    PRIMARY KEY,
            user_id   INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
            criado_em TEXT    NOT NULL,
            expira_em REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sessoes_user ON sessoes(user_id);
        CREATE INDEX IF NOT EXISTS idx_sessoes_expira ON sessoes(expira_em);

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

        -- Log de auditoria: quem fez o quê no painel administrativo. `usuario_id` fica
        -- NULL pra tentativa de login falha (não há "quem" autenticado ainda) — o alvo
        -- (e-mail tentado) vai em `detalhe`. `usuario_nome` é denormalizado de propósito:
        -- o nome de quem agiu tem que sobreviver mesmo que a conta seja desativada depois.
        CREATE TABLE IF NOT EXISTS auditoria (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            criado_em TEXT NOT NULL,
            usuario_id INTEGER,
            usuario_nome TEXT NOT NULL DEFAULT '',
            acao TEXT NOT NULL,
            alvo_tipo TEXT NOT NULL DEFAULT '',
            alvo_id TEXT NOT NULL DEFAULT '',
            detalhe TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_auditoria_criado ON auditoria(criado_em DESC);
        CREATE INDEX IF NOT EXISTS idx_auditoria_usuario ON auditoria(usuario_id);
        CREATE INDEX IF NOT EXISTS idx_auditoria_acao ON auditoria(acao);

        -- Tokens de "esqueci minha senha" / convite por e-mail (mesmo mecanismo pros
        -- dois casos — a diferença é só o texto do e-mail enviado). `usado`: token de
        -- uso único, marcado depois de trocar a senha; um token usado ou expirado não
        -- serve mais, mas a linha fica pra auditoria.
        CREATE TABLE IF NOT EXISTS reset_senha_tokens (
            token     TEXT    PRIMARY KEY,
            user_id   INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
            criado_em TEXT    NOT NULL,
            expira_em REAL    NOT NULL,
            usado     INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_reset_tokens_user ON reset_senha_tokens(user_id);
        """)
        _migrar(c)
