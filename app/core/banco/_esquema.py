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
    # Grau de consenso do loop de leitura (0..1). `confianca` é a confiança do OCR num
    # crop; `acordo` é quantos frames concordaram com a placa eleita — é ele que separa
    # uma leitura sólida de um chute devolvido por timeout. Sem essa coluna as duas
    # ficam indistinguíveis no histórico: quem audita uma cobrança contestada não tem
    # como saber se a placa foi consenso ou palpite. Fica NULL para detecções do
    # pipeline ao vivo, que não passam pelo loop de consenso.
    if "acordo" not in cols_det:
        c.execute("ALTER TABLE deteccoes ADD COLUMN acordo REAL")
    # Veredito congelado NO MOMENTO DA GRAVAÇÃO: 1 = acordo atingiu o mínimo, 0 = ficou
    # abaixo (leitura fraca, devolvida por timeout). Guardado em vez de recalculado a
    # partir de `acordo` porque `leitura_acordo_minimo` é configurável: se o limiar mudar
    # amanhã, uma cobrança contestada de hoje precisa ser julgada pelo critério que valia
    # quando foi gravada. NULL = não se aplica (pipeline ao vivo) ou linha anterior a esta
    # migração, casos em que o consenso é desconhecido — nunca presumir confirmada.
    if "confirmada" not in cols_det:
        c.execute("ALTER TABLE deteccoes ADD COLUMN confirmada INTEGER")
    # 'moto' | 'carro' | NULL. NÃO dá para derivar isto de `padrao`, que guarda o FORMATO
    # da placa (mercosul/antigo) e vale para os dois tipos de veículo — sem coluna própria
    # o histórico não tem como separar moto de carro.
    #
    # O valor é a CLASSE do detector de veículo (YOLOX, 1º estágio da detecção em 2
    # estágios): COCO 3=motorcycle → 'moto'; 2/5/7=car/bus/truck → 'carro'. Ela viaja
    # carregada na própria bbox da placa, desde `DetectorDoisEstagios`, porque a associação
    # é estrutural — a placa foi encontrada DENTRO do recorte daquele veículo.
    #
    # Continua sendo estimativa, não cadastro, e a interface rotula como "estimado".
    #
    # Até 20/08/2026 a fonte era o `e_moto` do AutoOCR (`tinha_header and aspect <= 2.0`).
    # Aposentada por medição: 32,8% das 774 detecções reais tinham aspecto abaixo do
    # limiar, 12 dos 25 rótulos gravados eram 'moto' num posto de combustível (11 depois
    # refutados rodando o YOLOX nos quadros salvos), e o mesmo veículo recebeu vereditos
    # opostos com 3 min de diferença — bbox 59×27 deu 'carro', 56×28 deu 'moto'. O aspecto
    # do bbox mede a folga do detector, não a diagramação da placa, então não havia limiar
    # que consertasse.
    #
    # NULL = desconhecido, e nenhum destes casos deve ser fundido com 'carro': linhas
    # anteriores à troca de fonte, 2 estágios desligado (`veiculo_dois_estagios_*=nao`),
    # nenhum veículo detectado no quadro, e placa recuperada pela varredura em janelas
    # (que roda só o estágio de placa).
    if "tipo_veiculo" not in cols_det:
        c.execute("ALTER TABLE deteccoes ADD COLUMN tipo_veiculo TEXT")
    # O SINAL CRU por trás do veredito acima — mesmo precedente de `acordo`+`confirmada`
    # (medida crua + veredito congelado), agora aplicado a `tipo_veiculo`. `veiculo_classe`
    # é a classe COCO bruta (2=car, 3=motorcycle, 5=bus, 7=truck — não só 'carro'/'moto'),
    # `veiculo_conf` é a confiança do detector de VEÍCULO (não confundir com `confianca`,
    # que é do OCR). Sem isto, "subir `veiculo_conf` de 0,4 para 0,5 custaria o quê?" só
    # tinha resposta por palpite — medido: perderia ~14,7% dos veículos vistos.
    #
    # `tipo_veiculo_fonte` é o motivo, inclusive quando `tipo_veiculo` é NULL — hoje essa
    # coluna (acima) já documenta 4 causas de NULL em texto corrido; esta as torna
    # consultáveis. Vocabulário (cresce; validado em Python em `_deteccoes.py`, não por
    # CHECK, que não dá para estender sem recriar a tabela):
    #   'veiculo'             tipo veio de um veículo detectado
    #   'classe-nao-mapeada'  veículo detectado, classe fora do mapa (ex.: config custom)
    #   'sem-veiculo'         2 estágios rodou, nenhum veículo no quadro
    #   'tiles'               placa veio da varredura em janelas (não roda estágio de veículo)
    #   'sem-2-estagios'      detector de 1 estágio
    #   'replay:<causa>'      reconstruído a posteriori por `testes/recalcula_tipo_veiculo.py`
    #                         a partir do quadro salvo — nunca confundir com medida ao vivo
    # NULL nesta coluna = linha anterior a esta migração (o veredito em `tipo_veiculo`,
    # se houver, não tem sinal cru correspondente).
    if "veiculo_classe" not in cols_det:
        c.execute("ALTER TABLE deteccoes ADD COLUMN veiculo_classe INTEGER")
    if "veiculo_conf" not in cols_det:
        c.execute("ALTER TABLE deteccoes ADD COLUMN veiculo_conf REAL")
    if "tipo_veiculo_fonte" not in cols_det:
        c.execute("ALTER TABLE deteccoes ADD COLUMN tipo_veiculo_fonte TEXT")
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

    # ── Segunda câmera por bico (opcional) ───────────────────────────────────────────
    # A câmera do posto fica elevada: um veículo com estepe/roda na traseira esconde a
    # placa traseira, e aí NENHUM ajuste de OCR resolve — não há pixel de placa no frame
    # (é o que a medição de 13/08/2026 mostrou: o gargalo dominante é enquadramento, não
    # leitura). Uma segunda câmera enxergando o outro lado do carro é a única saída.
    #
    # `camera2_id` NULL = bico de uma câmera, comportamento de sempre — a feature é
    # opcional bico a bico, não uma migração de todo mundo. Nullable também é o que
    # permite o `ADD COLUMN ... REFERENCES` (o SQLite só aceita a cláusula quando o
    # default é NULL; mesmo caso de `cameras.empresa_id` e `usuarios.empresa_id` acima).
    #
    # `roi2` é coluna própria, e não um segundo campo no mesmo JSON, porque o ROI está em
    # COORDENADAS DO FRAME de uma câmera específica: o retângulo da traseira não quer
    # dizer nada na imagem da frente.
    #
    # Os papéis são descritivos (rótulo na tela + de qual ângulo saiu a placa no
    # histórico); nenhuma decisão do laço de leitura depende deles. Linhas existentes
    # ficam 'traseira' porque é o que as câmeras já instaladas enquadram — o palpite é
    # certo na esmagadora maioria e corrigível em dois cliques, enquanto um valor
    # "indefinido" obrigaria o admin a revisitar todos os bicos sem ganhar informação.
    cols_bic = {row[1] for row in c.execute("PRAGMA table_info(bicos)").fetchall()}
    if "papel_camera" not in cols_bic:
        c.execute("ALTER TABLE bicos ADD COLUMN papel_camera TEXT NOT NULL DEFAULT 'traseira'")
    if "camera2_id" not in cols_bic:
        c.execute("ALTER TABLE bicos ADD COLUMN camera2_id INTEGER REFERENCES cameras(id) ON DELETE RESTRICT")
    if "roi2" not in cols_bic:
        c.execute("ALTER TABLE bicos ADD COLUMN roi2 TEXT")
    if "papel_camera2" not in cols_bic:
        c.execute("ALTER TABLE bicos ADD COLUMN papel_camera2 TEXT NOT NULL DEFAULT 'frente'")
    # Fica aqui (não no CREATE inicial) pelo mesmo motivo de `idx_deteccoes_bico`: só
    # depois desta migração a coluna existe também nos bancos antigos.
    c.execute("CREATE INDEX IF NOT EXISTS idx_bicos_camera2 ON bicos(camera2_id)")

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

        -- Um bico enxerga o veículo por 1 ou 2 câmeras (ver `camera2_id` abaixo). Cada
        -- câmera tem ROI e papel PRÓPRIOS: o retângulo está em coordenadas do frame
        -- daquela câmera e não significa nada na outra.
        CREATE TABLE IF NOT EXISTS bicos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            automacao_id INTEGER NOT NULL REFERENCES automacoes(id) ON DELETE CASCADE,
            codigo TEXT NOT NULL,                          -- chave usada na resolução do GET
            nome TEXT NOT NULL DEFAULT '',
            bomba INTEGER,                                 -- opcional, só organização/UI
            lado INTEGER,                                  -- opcional, só organização/UI
            camera_id INTEGER NOT NULL REFERENCES cameras(id) ON DELETE RESTRICT,
            roi TEXT,                                      -- {x,y,w,h} — área própria deste bico
            papel_camera TEXT NOT NULL DEFAULT 'traseira', -- 'traseira' | 'frente'
            camera2_id INTEGER REFERENCES cameras(id) ON DELETE RESTRICT,   -- NULL = uma câmera só
            roi2 TEXT,
            papel_camera2 TEXT NOT NULL DEFAULT 'frente',
            ativo INTEGER NOT NULL DEFAULT 1,
            criado_em TEXT NOT NULL,
            UNIQUE(automacao_id, codigo)
        );
        CREATE INDEX IF NOT EXISTS idx_bicos_automacao ON bicos(automacao_id);
        CREATE INDEX IF NOT EXISTS idx_bicos_camera ON bicos(camera_id);
        -- `idx_bicos_camera2` NÃO entra aqui: num banco antigo a coluna `camera2_id` só
        -- existe depois de `_migrar` (que roda no fim de `inicializar`), e o CREATE TABLE
        -- IF NOT EXISTS acima não altera uma tabela que já existe. Criar o índice neste
        -- script quebra o boot de toda instalação existente com "no such column". Mesmo
        -- motivo de `idx_deteccoes_bico` — ver o comentário dele em `_migrar`.

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
            status TEXT NOT NULL,        -- ok | nao_confirmada | sem_placa | erro_cadastro | erro_camera
                                         -- nao_confirmada: devolveu placa, mas o acordo
                                         -- ficou abaixo de leitura_acordo_minimo
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

        -- ── Cache dos dados de veículo consultados na apiplacas.com.br ──────────
        -- Existe por DINHEIRO: cada consulta à API externa custa crédito pré-pago, e
        -- marca/modelo/combustível de um veículo NUNCA mudam — o posto repete as mesmas
        -- placas todo dia (frota, cliente fiel, aplicativo). Sem esta tabela, pagaríamos
        -- de novo pelo mesmo dado imutável em cada abastecimento.
        --
        -- SEM coluna de posto/empresa DE PROPÓSITO: o cache é compartilhado por todo o
        -- servidor. Placa não é dado de um cliente nosso, é dado do VEÍCULO, e a mesma
        -- frota circula por vários postos da mesma rede — escopar por CNPJ multiplicaria
        -- a conta pelo número de postos sem ganhar nada.
        --
        -- NÃO é purgada pela retenção (`app/operacao/retencao.py` só conhece `deteccoes`
        -- e `chamadas`). É intencional: purgar o cache reintroduz exatamente o custo que
        -- ele existe para eliminar. Não há imagem nem vínculo com abastecimento aqui, e
        -- os dados são do veículo (marca, modelo, município), não do proprietário — o
        -- `chassi` vem mascarado pelo próprio fornecedor e não é extraído para coluna.
        CREATE TABLE IF NOT EXISTS veiculos (
            -- NORMALIZADA (maiúscula, só alfanumérico) antes de gravar E de ler. A
            -- comparação de TEXT PRIMARY KEY no SQLite é BINÁRIA: "abc1d23" e "ABC1D23"
            -- seriam duas linhas e DUAS COBRANÇAS pelo mesmo veículo. Quem normaliza é
            -- `app/integracoes/apiplacas.py`, num lugar só.
            placa TEXT PRIMARY KEY,

            -- Veredito sobre o VEÍCULO, não sobre a chamada HTTP:
            --   'ok'          a API respondeu 200 e há dado (que pode estar incompleto)
            --   'inexistente' a API respondeu 406 "sem resultados" — a placa não consta
            --                 na base consultada. É resposta LEGÍTIMA e precisa ser
            --                 cacheada: o caso comum é OCR que leu errado, uma placa que
            --                 nunca vai existir e que sem isso seria reconsultada (e
            --                 recobrada) em todo abastecimento daquele bico.
            -- Falha de rede/timeout/402/429 NÃO viram linha aqui: não são respostas sobre
            -- o veículo, e gravá-las faria uma indisponibilidade passageira "responder"
            -- pelos próximos 180 dias. Vocabulário validado em Python (`_veiculos.py`) e
            -- não por CHECK — mesmo motivo de `tipo_veiculo_fonte`: um CHECK não dá para
            -- estender sem recriar a tabela.
            status TEXT NOT NULL,

            criado_em     TEXT NOT NULL,   -- 1ª vez que se pagou por esta placa
            consultado_em TEXT NOT NULL,   -- data da resposta ARMAZENADA; base do TTL
            -- Quantas vezes a API PAGA foi chamada para esta placa (1 na primeira, +1 a
            -- cada reconsulta por TTL vencido). É contabilidade de gasto que sobrevive a
            -- restart do processo, ao contrário do freio em memória do `limitador`.
            consultas INTEGER NOT NULL DEFAULT 1,
            http_status INTEGER,           -- 200 | 406. NULL = linha importada à mão

            -- ── Bloco curado: o que vai no payload do roteador ──────────────────
            -- TODAS nullable, e NULL significa "a API não informou" — NUNCA um valor por
            -- omissão. A doc da apiplacas avisa que o objeto `extra` (de onde sai o
            -- combustível) pode vir AUSENTE OU INCOMPLETO, e que `fipe` pode faltar.
            -- Preencher 'Gasolina' por padrão seria inventar o dado mais importante da
            -- integração — e num flex, inventá-lo errado.
            combustivel       TEXT,   -- extra.combustivel, ex. "Alcool / Gasolina" — o alvo
            combustivel_sigla TEXT,   -- fipe (maior score).sigla_combustivel, ex. "G"
            marca             TEXT,
            modelo            TEXT,
            ano               INTEGER,
            ano_modelo        INTEGER,
            cor               TEXT,
            especie           TEXT,   -- extra.especie, ex. "Passageiro"
            -- extra.tipo_veiculo, ex. "Automovel"/"Motocicleta". VOCABULÁRIO DO
            -- FORNECEDOR: não confundir com `deteccoes.tipo_veiculo` ('moto'/'carro'),
            -- que é estimativa NOSSA do detector. São escalas diferentes e cruzá-las
            -- faria alguém "corrigir" nosso detector com dado de terceiro.
            tipo_veiculo      TEXT,
            -- É o campo VOLÁTIL do bloco, e o que motiva o TTL não ser infinito. Com TTL
            -- de 180 dias pode estar 180 dias velho: NÃO serve como checagem de
            -- roubo/restrição em tempo real. Está documentado em INTEGRACAO_ROTEADOR.md.
            situacao          TEXT,
            municipio         TEXT,
            uf                TEXT,

            -- Resposta 200 INTEIRA, como veio. É o que permite expor amanhã chassi,
            -- cilindradas, quantidade_passageiro, segmento ou FIPE SEM PAGAR DE NOVO
            -- pelas placas já consultadas. Sem ele, cada campo novo pedido pelo posto
            -- custaria uma recompra do histórico todo. NULL nas negativas (sem corpo útil).
            bruto TEXT,

            -- De qual provedor veio. Existe para o dia em que houver um segundo, ou um
            -- import manual — para não confundir dado pago com dado digitado.
            fonte TEXT NOT NULL DEFAULT 'apiplacas'
        );
        -- Serve ao teto DIÁRIO de gasto (COUNT de linhas com consultado_em >= meia-noite)
        -- e à varredura "reconsultar as mais velhas". A busca do caminho quente é pela
        -- PK, que já tem índice próprio. Ambos podem ficar NESTE script (e não em
        -- `_migrar`) porque a tabela inteira nasce aqui: num banco antigo ela é criada já
        -- completa, então não há o risco de "no such column" que manda
        -- `idx_bicos_camera2`/`idx_deteccoes_bico` para o `_migrar`.
        CREATE INDEX IF NOT EXISTS idx_veiculos_consultado ON veiculos(consultado_em);
        CREATE INDEX IF NOT EXISTS idx_veiculos_status ON veiculos(status);
        """)
        _migrar(c)
