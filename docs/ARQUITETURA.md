# Arquitetura do Sistema ALPR

> Estado atual · julho/2026 · reorganizado em pacote `app/` (core/visao/streaming/operacao/seguranca/web) · sincronizado com o código.

> ⚠️ **Este documento descreve a fase single-camera/diagnóstico do projeto (YOLO26n,
> foco em 1 câmera local) e não foi atualizado linha a linha desde então.** O modo de
> operação ALVO hoje é outro: servidor central **reativo multi-tenant** (`GET
> /api/leitura` por entidade→posto/CNPJ→automação→bico, ver [README.md](../README.md)
> e [INTEGRACAO_ROTEADOR.md](INTEGRACAO_ROTEADOR.md)), detector padrão
> `open_image_models` (não YOLO26n — ver `docs/`/memória do projeto sobre
> incompatibilidade do YOLO26 com a versão atual do ultralytics), e cadastro com 7
> tabelas (`entidades`, `empresas`, `automacoes`, `bicos`, `cameras`, `deteccoes`,
> `chamadas`), não as 5 listadas na seção 19. O pipeline contínuo descrito abaixo
> (seções 1–18) continua existindo e correto tecnicamente — é o modo "diagnóstico
> visual", opcional e inerte por padrão — só não é mais o caminho principal. A seção
> 20, abaixo, cobre o que este documento ainda não tinha: capacidade e escala do modo
> multi-tenant.

---

## 1. Visão Geral

Sistema de reconhecimento automático de placas (ALPR) rodando em Python com FastAPI, YOLO26n ONNX e suporte a múltiplos engines OCR. Desenvolvido no Windows, direcionado também para produção em Raspberry Pi ARM64.

```
┌──────────────────────────────────────────────────────────────────┐
│                       app/servidor.py                              │
│                FastAPI + Uvicorn (porta 14000)                    │
│  ┌──────────┐  ┌────────────┐  ┌────────────┐  ┌─────────────┐  │
│  │  paginas │  │   auth     │  │   stream   │  │  api (REST) │  │
│  │          app/web/*                                          │
│  └──────────┘  └────────────┘  └────────────┘  └─────────────┘  │
└─────────────────────────┬────────────────────────────────────────┘
                           │ lê / escreve
                    ┌──────▼──────┐
                    │ app/core/   │  ← fps, logs, detecções recentes,
                    │ estado.py   │     frames por câmera, crop OCR
                    │ (memória compartilhada)
                    └──────┬──────┘
                           │
      ┌────────────────────▼──────────────────────────────────────┐
      │              app/visao/pipeline.py                         │
      │  _instancias: dict[int, Pipeline]  (1 por câmera)         │
      │                                                            │
      │  Pipeline A  Pipeline B  Pipeline C  ...                   │
      │  (cam id=1)  (cam id=2)  (cam id=3)                       │
      │     │            │           │                             │
      │  Camera       Camera      Camera                           │
      │  ROI crop     ROI crop    ROI crop   ← área configurável   │
      │  Detector     Detector    Detector                         │
      │  Tracker      Tracker     Tracker    ← IoU / ByteTrack     │
      │  OCR          OCR         OCR         (app/visao/ocr/)      │
      │  Validador    Validador   Validador                        │
      │  Banco        Banco       Banco       (app/core/banco.py)   │
      └────────────────────────────────────────────────────────────┘
                           │ supervisiona
                    ┌──────▼──────────┐
                    │ app/operacao/   │  ← liveness + frame freshness
                    │ supervisor.py   │     (WorkerSupervisor) backoff exponencial
                    └─────────────────┘

         app/streaming/hls_encoder.py (opcional)
         FFmpeg por câmera → hls/{id}/*.ts + index.m3u8
```

Cada câmera cadastrada no banco recebe sua própria instância de `Pipeline` rodando em thread dedicada. O `WorkerSupervisor` monitora liveness e frescor de frame, reiniciando pipelines mortos com backoff exponencial (5 s → 300 s). As instâncias são gerenciadas pelo dicionário `_instancias: dict[int, Pipeline]` em `app/visao/pipeline.py`.

O código de produção mora em `app/` (execução: `python -m app.main`, a partir da raiz do repo). Assets (`templates/`, `static/`) vivem em `app/web/`; dados de runtime (`config.txt`, `placas.db`, `models/`, `hls/`) permanecem na raiz do repositório.

---

## 2. Módulos e Responsabilidades

| Arquivo | Responsabilidade |
|---------|-----------------|
| `app/main.py` | Ponto de entrada — argparse (`--reload`), suprime warnings PyTorch, delega para `app.servidor.iniciar()` |
| `app/servidor.py` | Cria app FastAPI, lifespan, monta rotas, monta `/hls` como static, registra MIME types, verifica porta antes de subir, exibe banner |
| `app/core/config.py` | Lê/grava `config.txt` (chave=valor) + override via env vars; padrão `intelbras` |
| `app/core/estado.py` | Estado global compartilhado entre threads (lock único); ring buffer de logs; `frames_cameras: dict[int, frame]` |
| `app/core/banco.py` | Camada SQLite — detecções, listas, câmeras, usuários, sessões; WAL mode; migração incremental |
| `app/core/broadcaster.py` | Hub WebSocket — eventos do pipeline (thread síncrona) → clientes conectados (loop asyncio) |
| `app/seguranca/sessao.py` | Hash de senhas (bcrypt) e sessões em memória com TTL |
| `app/visao/camera.py` | OpenCV VideoCapture: USB (CAP_DSHOW/CAP_V4L2), CSI, RTSP, Intelbras; auto-detecção backend no Windows |
| `app/visao/detector.py` | YOLO/open-image-models ONNX Runtime; auto-detecta formato de saída; fallback por contornos Canny |
| `app/visao/hardware.py` | Detecção de GPU/CUDA — providers ONNX Runtime e `torch_cuda_disponivel()` |
| `app/visao/ambiente.py` | Ajuste adaptativo de imagem por condição de ambiente (no-op se desativado) |
| `app/visao/ocr/engines.py` | Classe `OCR` — Tesseract, EasyOCR, PaddleOCR, docTR, fast-plate-ocr; pré-processamento multicamada |
| `app/visao/ocr/auto.py` | `AutoOCR`, `AutoOCRPaddle` (seleção automática) e `MultiOCR` (votação); `obter_ocr_leitura()` |
| `app/visao/ocr/__init__.py` | Fachada do pacote — reexporta `OCR`, `AutoOCR`, `AutoOCRPaddle`, `MultiOCR`, `obter_ocr_leitura` |
| `app/visao/validador.py` | Regex + correções posicionais (O↔0, I↔1, T↔7, B↔8…); janela deslizante; `formato_hint` |
| `app/visao/pipeline.py` | Loop principal por câmera; ROI crop; rate-limit; detecção; tracker; consenso; cooldown |
| `app/visao/tracker.py` | IoU Tracker interno + wrapper ByteTrack (boxmot); voto por track; reduz OCR em ~80–99% |
| `app/operacao/supervisor.py` | WorkerSupervisor — monitora liveness de threads e frescor de frame; reinicia com backoff exponencial |
| `app/operacao/dns_server.py` | Servidor DNS local embutido (stdlib) — resolve hostname configurado para o IP da LAN |
| `app/streaming/hls_encoder.py` | HLSManager — subprocess FFmpeg por câmera; codifica uma vez para N viewers; segmentos `.ts` + `.m3u8` |
| `app/streaming/stream.py` | Gerador MJPEG global e por câmera; snapshot JPEG |
| `app/web/api.py` | API REST completa: detecções, stats, health, listas, config, câmeras, ROI, debug crop |
| `app/web/auth.py` | Rotas de autenticação: login, logout, criar-admin; sessão via cookie HttpOnly 7 dias |
| `app/web/paginas.py` | Páginas HTML via Jinja2 (inclui `/roi/{camera_id}`) |
| `app/web/stream.py` | Endpoints `/stream.mjpg`, `/stream/{id}.mjpg`, `/snapshot.jpg` |
| `app/web/testes.py` | Avaliação de acurácia OCR e capturador de fotos de teste via UI |
| `app/web/templates/` | Templates Jinja2 (movido de `templates/` na raiz) |
| `app/web/static/` | Estáticos servidos em `/static` — inclui `snapshots/` (movido de `static/` na raiz) |

---

## 3. Fluxo de Dados

```
[Câmera — RTSP/USB/CSI/Intelbras]
   │  frame bruto (BGR NumPy)
   ▼
[Pipeline._loop — thread por câmera]
   ├─ rate-limit: sleep para manter taxa = camera_fps
   ├─ todo frame → estado.registrar_frame(frame)             → stream global
   │              → estado.registrar_frame_camera(id, frame) → stream /stream/{id}.mjpg
   └─ a cada intervalo (1 / deteccao_fps_max):
        ▼
   [ROI crop] — se roi configurado:
     └─ frame_det = frame[ry:ry+rh, rx:rx+rw]
        bboxes deslocadas de volta às coordenadas do frame completo após detecção
        ▼
   [Detector.detectar] — YOLO26n ONNX → bboxes [(x,y,w,h,conf)]
        ▼
   [Tracker.update(bboxes, frame)] — IoU / ByteTrack
     ├─ atribui track_id por IoU
     ├─ Tracker.precisa_ocr(track_id) → False se OCR já feito recentemente
     └─ pula OCR se não precisar → reduz chamadas em ~80–99%
        ▼  (apenas quando precisa_ocr=True)
   [_expandir_bbox]
     ├─ +5% esquerda, direita e base (BBOX_PADDING=0.05)
     └─ NÃO expande para cima
        ▼
   [OCR.ler(crop)] — pipeline de pré-processamento multicamada:
     ├─ _deskew(crop)                     ← correção de rotação 2D (minAreaRect)
     ├─ _corrigir_perspectiva(crop)       ← warp de 4 pontos (se quadrilátero encontrado)
     ├─ _remover_header(crop)
     ├─ _remover_ruidos_mercosul(crop)  (se header detectado)
     ├─ _focar_caracteres(crop)
     └─ [engine selecionado]
        ▼
   [validador.validar(texto, formato_hint)]
        ▼
   [Tracker.registrar_ocr + placa_pronta] — acumula votos por track_id
        ▼  (quando votos_emitir atingido)
   [_tentar_emitir]
     ├─ cooldown por placa (cooldown_seg, padrão=30s)
     ├─ banco.registrar_deteccao (SQLite WAL)
     ├─ estado.adicionar_deteccao (feed dashboard ao vivo)
     ├─ salvar snapshot JPEG (se salvar_snapshot=sim) — SÍNCRONO ⚠
     ├─ webhook todas detecções (se webhook_todas=sim) — SÍNCRONO ⚠
     └─ alerta lista negra (webhook) — SÍNCRONO ⚠
```

**Ordem de execução no loop:**
```python
# Detecção limitada por intervalo de tempo
if agora - self._ultima_deteccao >= self._intervalo_deteccao:
    self._processar_frame(frame)
    self._ultima_deteccao = agora

estado.registrar_frame(frame)
estado.registrar_frame_camera(id, frame)

# Rate-limit
decorrido = time.time() - t_loop
restante = _intervalo_loop - decorrido
if restante > 0.001:
    time.sleep(restante)
```

---

## 4. Banco de Dados

### Tabela `deteccoes`

| Coluna | Tipo | Descrição |
|--------|------|-----------|
| `id` | INTEGER PK | Auto-increment |
| `placa` | TEXT | Texto normalizado (7 chars, sem hífen) |
| `padrao` | TEXT | `mercosul` / `antigo` |
| `confianca` | REAL | 0.0–1.0 |
| `snapshot` | TEXT | `/static/snapshots/TIMESTAMP_PLACA.jpg` |
| `criado_em` | TEXT | ISO 8601 UTC |
| `camera_id` | TEXT | Tipo da câmera |
| `bbox` | TEXT | JSON `{x, y, w, h}` |

Índices: `placa` e `criado_em DESC`.

### Tabela `listas_placas`

| Coluna | Tipo | Descrição |
|--------|------|-----------|
| `id` | INTEGER PK | |
| `placa` | TEXT UNIQUE | |
| `tipo` | TEXT | `branca` / `negra` |
| `descricao` | TEXT | Observação livre |
| `criado_em` | TEXT | ISO 8601 UTC |

### Tabela `cameras`

| Coluna | Tipo | Descrição |
|--------|------|-----------|
| `id` | INTEGER PK | |
| `nome` | TEXT | Nome descritivo |
| `bomba` | INTEGER | Número da bomba |
| `lado` | INTEGER | Número do lado |
| `camera_tipo` | TEXT | `intelbras`, `usb`, `rtsp`, `csi` |
| `camera_indice` | TEXT | Índice USB ou URL RTSP manual |
| `intelbras_host` | TEXT | IP da câmera |
| `intelbras_porta` | TEXT | Porta RTSP (padrão `554`) |
| `intelbras_usuario` | TEXT | Usuário |
| `intelbras_senha` | TEXT | Senha (texto; gitignored) |
| `intelbras_canal` | TEXT | Canal RTSP |
| `intelbras_subtype` | TEXT | `0` = main, `1` = sub |
| `intelbras_formato` | TEXT | `padrao` ou `legado` |
| `rtsp_url_custom` | TEXT | URL RTSP completa (sobrepõe campos acima) |
| `roi` | TEXT | JSON `{x, y, w, h}` em pixels do frame — NULL = frame completo |
| `ativo` | INTEGER | 0/1 |
| `criado_em` | TEXT | ISO 8601 UTC |

Constraint: `UNIQUE(bomba, lado)`. Migração incremental via `_migrar()`.

### Tabela `usuarios`

| Coluna | Tipo | Descrição |
|--------|------|-----------|
| `id` | INTEGER PK | |
| `nome` | TEXT | Nome exibido |
| `email` | TEXT UNIQUE | Login |
| `senha_hash` | TEXT | bcrypt hash |
| `papel` | TEXT | `admin` |
| `criado_em` | TEXT | ISO 8601 UTC |

### Tabela `sessoes`

| Coluna | Tipo | Descrição |
|--------|------|-----------|
| `token` | TEXT PK | UUID hex 32 chars |
| `usuario_id` | INTEGER FK | → usuarios.id |
| `criado_em` | TEXT | ISO 8601 UTC |

WAL mode ativo em todas as tabelas.

---

## 5. Endpoints

### Páginas HTML

| Método | Rota | Página |
|--------|------|--------|
| GET | `/` | Stream ao vivo + feed de detecções |
| GET | `/dashboard` | Status, logs, saúde das câmeras, debug OCR crop |
| GET | `/historico` | Histórico paginado com filtros |
| GET | `/listas` | Cadastro de listas branca/negra |
| GET | `/configuracao` | Interface de configuração + teste de câmera |
| GET | `/cameras` | Cadastro, gerenciamento e scan de câmeras |
| GET | `/roi/{camera_id}` | Editor visual de área de captura (ROI) por câmera |
| GET | `/login` | Página de login |
| GET | `/criar-admin` | Criação do primeiro usuário administrador |
| GET | `/testes` | Avaliação de acurácia OCR |
| GET | `/documentacao` | Documentação da API |
| GET | `/setup` | Wizard de configuração inicial |

### Streams

| Método | Rota | Descrição |
|--------|------|-----------|
| GET | `/stream.mjpg` | Stream MJPEG global |
| GET | `/stream/{camera_id}.mjpg` | Stream MJPEG de câmera específica com bboxes |
| GET | `/snapshot.jpg` | Frame atual em JPEG |
| GET | `/hls/{camera_id}/index.m3u8` | Playlist HLS (modo `streaming_modo=hls`) |
| GET | `/hls/{camera_id}/*.ts` | Segmentos HLS |

### API REST

| Método | Rota | Descrição |
|--------|------|-----------|
| GET | `/api/placa/{placa}` | Consulta consolidada: última detecção, lista branca/negra, histórico |
| GET | `/api/deteccoes` | Lista paginada (placa, desde, ate, limit, offset) |
| DELETE | `/api/deteccoes/{id}` | Remove detecção |
| GET | `/api/stats` | Total, hoje, top 10 placas, fps, uptime, streaming_modo |
| GET | `/api/status` | Pipeline, fps, uptime, frames, câmera, modelo, OCR engine |
| GET | `/api/health` | Saúde por câmera: status, restarts, backoff, última detecção |
| GET | `/api/logs` | Logs em memória (filtrável por nível) |
| DELETE | `/api/logs` | Limpa buffer de logs |
| GET | `/api/recentes` | Últimas 20 detecções (buffer circular) |
| GET | `/api/listas` | Lista branca/negra |
| POST | `/api/listas` | Cadastra placa na lista |
| DELETE | `/api/listas/{id}` | Remove placa da lista |
| GET | `/api/config` | Lê configuração (senhas mascaradas) |
| POST | `/api/config` | Salva config e reinicia pipeline |
| POST | `/api/camera/teste` | Testa câmera com parâmetros ad-hoc → snapshot JPEG |
| GET | `/api/cameras` | Lista câmeras cadastradas |
| POST | `/api/cameras` | Cadastra câmera e inicia pipeline |
| PUT | `/api/cameras/{id}` | Atualiza câmera e reinicia pipeline |
| DELETE | `/api/cameras/{id}` | Remove câmera e para pipeline |
| POST | `/api/cameras/{id}/teste` | Snapshot da câmera (reutiliza frame do pipeline se ativo) |
| POST | `/api/cameras/{id}/ler-placa` | Captura frames, detecta e lê placa; retorna bomba, lado, confiança |
| GET | `/api/cameras/{id}/snapshot` | Retorna frame atual da câmera como JPEG |
| PUT | `/api/cameras/{id}/roi` | Salva ROI `{x, y, w, h}` e aplica imediatamente |
| DELETE | `/api/cameras/{id}/roi` | Remove ROI (volta a monitorar frame completo) |
| GET | `/api/cameras/rede-local` | Descobre sub-rede local para scan RTSP |
| POST | `/api/cameras/scan` | Varre sub-rede em busca de câmeras (porta RTSP 554) |
| GET | `/api/debug/ocr_crop` | Último crop enviado ao OCR |
| GET | `/modelos` | Lista arquivos `.onnx` disponíveis em `models/` |
| POST | `/setup/concluir` | Finaliza wizard de setup (`implantado=sim`) |

---

## 6. AutoOCR — Seleção Automática de Engine

O modo `auto` (padrão) usa dois engines em paralelo e escolhe o melhor resultado:

```
crop recebido
 ├─ _remover_header(crop)
 │    └─ tinha_header=True e e_mercosul=True e aspect > 2.0 → Mercosul carro
 │    └─ tinha_header=True e aspect ≤ 2.0                   → Moto Mercosul
 │    └─ tinha_header=False                                 → Antigo (carro ou moto)
 │
 ├─ Mercosul carro: principal=fast_plate_ocr, fallback=easyocr
 │     aceita sem fallback se conf ≥ 0.50
 ├─ Moto Mercosul: principal=easyocr, fallback=fast_plate_ocr
 │     SEMPRE compara os dois (layout 2 linhas é mais difícil)
 └─ Antigo:        principal=easyocr, fallback=fast_plate_ocr
       aceita sem fallback se conf ≥ 0.50
```

---

## 7. Engines OCR

| Engine | Biblioteca | Peso | Nota |
|--------|-----------|------|------|
| `tesseract` | `pytesseract` + binário SO | ~50 MB | PSM 6→11 fallback |
| `easyocr` | `easyocr` + PyTorch | ~200 MB | Bom para placas; GPU opcional |
| `paddleocr` | `paddlepaddle` + `paddleocr` | ~80 MB | Ótimo para ARM64 |
| `doctr` | `python-doctr[torch]` | ~150 MB | Transformers Mindee |
| `fast_plate_ocr` | `fast-plate-ocr` (ONNX) | ~8 MB | Especializado em placas; mais leve |

---

## 8. Rastreamento (Tracker)

O `Tracker` reduz chamadas OCR associando detecções ao mesmo veículo entre frames:

```
Detector retorna bboxes
   ↓
Tracker.update(bboxes_xywh, frame)
   ├─ tenta ByteTrack (boxmot) se instalado
   └─ fallback para IoU interno (zero dependências externas)
        ↓
para cada track_id:
   Tracker.precisa_ocr(track_id)          ← CONTA a tentativa (ver teto abaixo)
   ├─ False → pula OCR (já identificado, ou OCR suspenso neste track)
   └─ True  → OCR + Tracker.registrar_ocr(track_id, placa, ...)
                   ↓
              Tracker.placa_pronta(track_id)
              ├─ None → aguarda mais votos
              └─ (placa, padrao, conf) → _tentar_emitir
```

**Impacto medido (benchmark):** redução de OCR de 80–99% dependendo de cooldown e FPS.

**Teto de tentativas sem leitura:** um track que gasta `tracker_max_ocr_sem_leitura`
tentativas sem produzir UMA leitura válida tem o OCR suspenso. É o perfil de texto de cena
— letreiro, adesivo, texto de piso —, que é caixa fixa no quadro e não sai dele: em
04/09/2026 a palavra ENTRADA rodou o ensemble hora após hora numa cam de posto. A primeira
leitura válida desarma o teto naquele track para sempre, e é isso que impede abandonar
carro parado na bomba com a placa momentaneamente ocluída.

**Configuração:** `tracker_ativo = sim`, `tracker_ocr_intervalo = 5`,
`tracker_votos_emitir = 2`, `tracker_max_ocr_sem_leitura = 60`

---

## 9. Supervisor (WorkerSupervisor)

Roda em thread daemon `alpr-supervisor`, verifica cada câmera a cada 5 s:

```
WorkerSupervisor._loop
  para cada camera_id em _instancias:
    ├─ thread viva? → não → reiniciar + backoff
    ├─ frame fresco? (< 15s) → não → log "sem_frame"
    └─ frame travado (> 30s) → reiniciar + backoff
  backoff: 5 → 10 → 20 → 40 → ... → 300 s
```

`/api/health` expõe `supervisor.health()`:
```json
{
  "cameras": {
    "1": { "status": "ok", "restarts": 0, "backoff_atual": 0, "ultima_deteccao": "..." },
    "2": { "status": "sem_frame", "restarts": 1, "backoff_atual": 10, "ultima_deteccao": null }
  }
}
```

---

## 10. Ciclo de Vida das Câmeras

```
servidor.lifespan
  ├─ hls_encoder.hls_manager.iniciar(cameras)  (se streaming_modo=hls)
  └─ pipeline.iniciar_cameras_db(cfg)
       └─ banco.cameras_listar() → filtra ativo=1
            └─ para cada câmera:
                 Pipeline(cfg_merged).iniciar()   ← thread daemon alpr-pipeline
                 _instancias[camera_db_id] = p
  └─ supervisor.WorkerSupervisor().iniciar(cfg)  ← thread daemon alpr-supervisor

POST /api/cameras          → banco → background iniciar_camera
PUT  /api/cameras/{id}     → banco → reiniciar_camera
DELETE /api/cameras/{id}   → banco → parar_camera
PUT  /api/cameras/{id}/roi → banco.camera_salvar_roi + pinst.roi = roi  (sem restart)
DELETE /api/cameras/{id}/roi → banco.camera_limpar_roi + pinst.roi = None (sem restart)
POST /api/config           → reiniciar(novo_cfg) → parar_todas + iniciar_cameras_db

servidor.lifespan (shutdown)
  ├─ pipeline.parar_todas()
  ├─ supervisor.parar()
  └─ hls_encoder.hls_manager.parar()
```

`_cfg_para_camera(global_cfg, cam)`: mescla config global com campos da câmera. Propaga `roi` se presente.

---

## 11. Autenticação

Toda rota (HTML e API) verifica o cookie `sessao`:

```
Request → dep_auth(request)
  ├─ cookie "sessao" presente?
  │    ├─ token válido em banco.sessions? → ok (injeta usuario)
  │    └─ token inválido → redirect /login (HTML) ou HTTP 401 (API)
  └─ sem cookie → redirect /login (HTML) ou HTTP 401 (API)

/login POST → bcrypt.checkpw → criar_sessao → Set-Cookie sessao (7 dias, HttpOnly)
/logout     → banco.remover_sessao → apagar cookie → redirect /login
/criar-admin → disponível apenas quando nenhum usuário existe no banco
```

Global fetch interceptor em `base.html` captura respostas HTTP 401 das rotas de API e redireciona para `/login`, evitando que mensagens de erro apareçam na UI.

---

## 12. ROI (Região de Interesse)

Cada câmera pode ter uma área de captura configurada via `/roi/{camera_id}`:

```
Frame bruto (ex: 1280×720)
   ↓
ROI crop: frame[ry:ry+rh, rx:rx+rw]   ← pixels do frame real
   ↓
Detector.detectar(frame_det)
   ↓
bboxes_roi → offset: (x+rx, y+ry, w, h, c)  ← volta a coord. do frame completo
   ↓
Stream anotado com bboxes nas posições corretas
```

O ROI é aplicado sem reiniciar o pipeline: `PUT /api/cameras/{id}/roi` atualiza `pinst.roi` em memória e persiste no banco. NULL = frame completo monitorado.

---

## 13. Detecção RTSP — Restrição Intelbras

Câmeras Intelbras aceitam **uma única conexão RTSP simultânea**. Por isso:
- `POST /api/cameras/{id}/teste`: reutiliza o frame em `estado.frames_cameras[id]` se pipeline ativo.
- Aguarda até 8s pelo primeiro frame antes de abrir nova conexão.

---

## 14. HLS Streaming (modo opcional)

Quando `streaming_modo = hls` em `config.txt`:

```
Pipeline → estado.frames_cameras[id] (numpy frame)
   ↓
hls_encoder._Encoder._alimentar()
   └─ pipe stdin do FFmpeg (rawvideo bgr24)
        ↓
   FFmpeg → hls/{id}/*.ts  (1s/seg, 6 segs buffer)
           → hls/{id}/index.m3u8
        ↓
   StaticFiles serve /hls/...
```

**Requisito:** FFmpeg instalado e no PATH. Detectado via `shutil.which("ffmpeg")`.  
**Fallback:** se FFmpeg ausente, lifespan registra aviso e continua em modo MJPEG.  
**Vantagem:** O(cameras) encodes — não O(cameras × viewers) como no MJPEG.

---

## 15. Pontos Fortes

- **Autenticação completa** — bcrypt + sessão via cookie; rota de criação bloqueada após primeiro admin.
- **ROI por câmera** — crop antes do YOLO, aplicado sem restart, configurável por interface visual.
- **Tracker com votação** — reduz OCR em 80–99%; ByteTrack ou IoU interno como fallback.
- **WorkerSupervisor** — reinicia pipelines mortos com backoff; health panel no dashboard.
- **HLS opcional** — encoda uma vez por câmera; N viewers sem custo extra.
- **Arquitetura multi-câmera real** — pipeline, thread e stream independentes por câmera.
- **Pré-processamento OCR em cadeia** — deskew → perspectiva → header removal → mascaramento QR/BR → foco nos caracteres.
- **Validador com janela deslizante** — recupera placa válida de texto > 7 chars.
- **Rate-limit duplo** — loop travado em `camera_fps`; detecção limitada por `deteccao_fps_max`.
- **Sem expansão para cima no bbox** — evita capturar faixa do cabeçalho Mercosul.
- **Verificação de porta antes de subir** — previne crash silencioso.
- **WAL mode SQLite** — leituras concorrentes sem bloquear pipeline.
- **Debug OCR crop ao vivo** — `/api/debug/ocr_crop` exibe exatamente o que o engine recebe.

---

## 16. Problemas Conhecidos / Pendências

### Críticos

#### Webhook bloqueia o pipeline por até 5s
`requests.post(timeout=5)` é chamado dentro de `_tentar_emitir`. Se o servidor externo estiver lento, o pipeline trava.

**Solução proposta:** fila `queue.Queue` + thread worker dedicada.

#### Snapshot em disco no hot path
`cv2.imwrite(...)` é chamado dentro de `_tentar_emitir` — I/O bloqueante.

**Solução proposta:** fila + worker de escrita assíncrona.

#### Conexão SQLite nova a cada query
`banco.cursor()` abre e fecha por operação.

**Solução proposta:** `threading.local()` com conexão persistente por thread.

### Moderados

#### Sem testes automatizados
Zero testes unitários ou de integração.

#### Credenciais em texto puro
`intelbras_senha` sem criptografia no banco. Mitigado por `config.txt` no `.gitignore`.

---

## 17. Diagrama de Dependências

```
app.main
 └─ app.servidor
     ├─ app.core.config ◄────────────────── (sem deps internas)
     ├─ app.core.banco
     ├─ app.core.estado ◄─────────────────── (sem deps internas)
     ├─ app.core.broadcaster
     ├─ app.seguranca.sessao ──► (sem deps internas)
     ├─ app.visao.pipeline
     │   ├─ app.visao.camera
     │   ├─ app.visao.detector ──► app.visao.hardware
     │   ├─ app.visao.tracker ──► (boxmot opcional)
     │   ├─ app.visao.ambiente ──► app.core.estado
     │   ├─ app.visao.ocr (pacote)
     │   │   ├─ engines.py ──► app.core.estado, app.visao.hardware
     │   │   └─ auto.py ──► engines.py, app.visao.validador
     │   ├─ app.visao.validador ◄──────────── (sem deps internas)
     │   └─ app.core.banco
     ├─ app.operacao.supervisor ──► app.visao.pipeline, app.core.estado
     ├─ app.operacao.dns_server ◄──────────── (sem deps internas)
     ├─ app.streaming.hls_encoder ──► app.core.estado (frames_cameras)
     ├─ app.web.api ──► app.core.banco, app.visao.camera, app.core.config,
     │                  app.core.estado, app.visao.pipeline, app.operacao.supervisor,
     │                  app.visao.detector, app.visao.ocr, app.visao.validador
     ├─ app.web.auth ──► app.core.banco, app.seguranca.sessao (templates: app/web/templates)
     ├─ app.web.paginas (templates: app/web/templates)
     ├─ app.web.stream ──► app.streaming.stream ──► app.core.estado
     └─ app.web.testes ──► app.core.banco, app.core.estado, app.core.config,
                           app.visao.camera, app.visao.pipeline
```

---

## 18. Câmera Intelbras (linha VIP)

### Formato padrão (maioria dos modelos VIP)

```
rtsp://USUARIO:SENHA@HOST:554/cam/realmonitor?channel=N&subtype=S
```

### Formato legado (VIP 1120/1220/1130)

```
rtsp://HOST:554/user=USUARIO&password=SENHA&channel=N&stream=S.sdp?
```

| Parâmetro | Descrição |
|-----------|-----------|
| `subtype` | `0` = main stream; `1` = sub stream (recomendado) |
| `channel` | Sempre `1` em câmera IP standalone |
| `formato` | `padrao` ou `legado` |
| `rtsp_url_custom` | URL completa que sobrepõe todos os campos |

**Boas práticas:**
1. Fixar IP da câmera no DHCP do roteador.
2. Criar usuário dedicado (não `admin`) com permissão só de stream.
3. Usar VLAN separada.
4. Verificar antes de subir: `ffplay -rtsp_transport tcp "rtsp://admin:SENHA@IP:554/cam/realmonitor?channel=1&subtype=1"`

---

## 19. Métricas Atuais

| Métrica | Valor |
|---------|-------|
| Arquivos Python principais | 21 (organizados em `app/core`, `app/visao`, `app/streaming`, `app/operacao`, `app/seguranca`, `app/web`) |
| Linhas de código (aprox.) | ~2 400 |
| Testes automatizados | dataset 42 placas + script de avaliação |
| Endpoints REST | 26 |
| Endpoints HTML | 12 |
| Endpoints de stream | 5 (MJPEG ×2, snapshot, HLS playlist, HLS segments) |
| Engines OCR suportadas | 5 |
| Tipos de câmera | 4 (USB, CSI, RTSP, Intelbras) |
| Formatos de placa reconhecidos | 2 (Mercosul, Antigo) — carro e moto |
| Tabelas no banco | 5 (deteccoes, listas_placas, cameras, usuarios, sessoes) |
| CPU em operação (estimativa) | 8–15% (era ~60% antes do rate-limit) |
| Precisão OCR (engine `auto`) | **92.9%** (39/42) no dataset de testes |
| Redução de chamadas OCR (tracker) | **80–99%** dependendo de FPS e cooldown |

---

## 20. Capacidade e escala (multi-tenant)

O modo alvo (seção "Visão Geral" acima) é **um processo Python só atendendo todos os
postos de todos os clientes** via `GET /api/leitura`. Isso tem uma implicação de
capacidade que as seções 1–19 (escritas para o modo diagnóstico single-camera) não
cobrem.

### O gargalo: lock global de detector/OCR

`app/visao/detector.py:detector_leitura_lock` e `app/visao/ocr/auto.py:ocr_leitura_lock`
serializam TODA leitura reativa do processo — não por câmera, por **todo o servidor**.
Isso não é um bug a corrigir: em `CUDAExecutionProvider` (GPU), chamadas concorrentes
`Run()` na mesma sessão onnxruntime podem travar/crashar (handles cuDNN compartilhados
entre threads) — o lock é o que torna isso seguro. A consequência é que duas leituras
de **clientes diferentes**, acionadas ao mesmo tempo, competem pelo mesmo recurso.

O loop de leitura (`app/visao/leitura.py:ler_placa`) é limitado por TEMPO
(`leitura_timeout_seg`), não por número fixo de fotos — então sob disputa, cada
chamada simplesmente consegue menos tentativas dentro do mesmo orçamento. A latência
quase não muda (por isso não aparece "no relógio"); quem cai é a taxa de acerto, e só
sob carga.

### Pergunta em aberto: quantos postos cabem por servidor?

`testes/medir_concorrencia.py` mede exatamente isso (dispara N leituras simultâneas
via `POST /api/bicos/{id}/ler-placa-teste` e compara tentativas/duração por nível de
concorrência) — mas **precisa rodar no servidor de produção real (GPU)** para dar uma
resposta que valha alguma coisa; medir em CPU de desenvolvimento não representa a
capacidade real. Enquanto esse número não existe, tratar como desconhecido — não
assumir que o servidor aguenta qualquer volume de clientes simultâneos.

### Quando esse teto aparecer, opções de escala (nenhuma implementada ainda)

- **Vertical primeiro**: GPU mais forte, mais VRAM — adia o problema, não resolve para
  sempre.
- **Múltiplos processos na mesma máquina**, cada um com sua própria sessão
  onnxruntime/lock — precisa de um roteador na frente (nginx/Traefik) decidindo qual
  processo atende qual `cnpj`, e um banco compartilhado (ou um banco por processo, com
  o custo de perder a visão consolidada num painel só).
- **Sharding de clientes por servidor** (réplicas completas — banco + processo — cada
  uma dona de um subconjunto de clientes): mais simples de operar que múltiplos
  processos numa máquina só, mas perde o "servidor central único" que é a proposta
  atual (o cadastro/painel deixa de ser um lugar só).
- **Fila de leitura com prioridade/timeout explícito** em vez de deixar o lock
  enfileirar tudo às cegas — devolve 503 rápido a quem está esperando demais, em vez
  de segurar a conexão até o `leitura_timeout_seg` estourar.

Nenhuma dessas foi decidida — a decisão certa depende do número que
`medir_concorrencia.py` trouxer.

---

## 21. RBAC: o que ficou de uma branch paralela e o que foi resolvido

Durante o desenvolvimento do RBAC (papel `admin` × `cliente` restrito a um posto —
seções acima), uma branch paralela (`feat: administração de usuários e testes`)
implementou, ao mesmo tempo e sem visibilidade de uma pra outra, uma **segunda**
solução de gestão de usuários. O merge das duas não gerou conflito de texto (os
dois trabalhos mexiam em arquivos/caminhos técnicamente diferentes), mas deixou
metade do código órfão — `app/core/banco.py` (modificado nesta sessão) coexistindo
com `app/core/banco/` (pacote da outra branch), com o pacote vencendo a resolução
de import do Python **silenciosamente**. Só apareceu ao rodar `pytest
testes/unitarios` pela primeira vez com sucesso — os 60 testes que a outra branch
trouxe testavam um desenho que tinha virado código morto.

Decisão, revisada teste a teste:

**Adotado** (portado para dentro de `app/core/banco/`, hoje em produção):
- **Sessões em SQLite** (`banco.sessao_*`) em vez de dict em memória —
  sobrevivem a restart do servidor e, mais importante pro momento (ver §20), abrem
  a porta pra rodar múltiplos workers uvicorn sem cada um ter sua própria sessão.
  `app/seguranca/sessao.py` manteve a mesma interface pública; ninguém que já
  chamava `criar_sessao`/`obter_user_id`/etc. precisou mudar.
- **`GET /api/usuarios/eu`** — "quem sou eu", usado pela UI e pelas travas abaixo.
- **Autoproteção**: um admin não consegue alterar o PRÓPRIO papel/status via
  `PUT /api/usuarios/{id}` (precisa pedir a outro admin) — trava a mais além de "não
  pode ser o último admin ativo", que sozinha só cobre o caso de sobrar zero.
- **Edição de nome/e-mail** no mesmo `PUT` (antes só papel/posto/ativo).
- **`app/seguranca/tentativas.py`** (freio de força bruta por e-mail+IP, com espera
  progressiva) substituiu o limitador genérico só-por-IP no `/login` — mais preciso
  contra os dois padrões de ataque (varrer e-mails de um IP, atacar um e-mail de
  vários IPs).
- **Papel `operador`** (adotado numa segunda passada, a pedido — não ligado a um
  posto específico, vê todos como admin, mas não passa em `deps.exigir_admin`):
  equipe interna que opera o painel no dia a dia sem poder reconfigurar o sistema.
  `deps.empresa_do_usuario` trata `operador` igual a `admin` (sem restrição de
  escopo) — a única diferença dos dois é `eh_admin`.
- **Troca de senha self-service** (`POST /api/usuarios/eu/senha`) — qualquer usuário
  logado (admin, operador ou cliente) troca a PRÓPRIA senha sem depender de um
  admin, mediante a senha atual. Isso obrigou a tirar o gate de admin do
  `include_router` de `/api/usuarios` (era bloqueio geral) e mover pra cada rota
  individualmente — `GET /api/usuarios/eu` e a troca de senha ficaram abertas a
  qualquer logado; listar/criar/editar outros usuários continuam admin-only.
  Página `/minha-conta` (link no nav, visível a todo mundo logado).

**Rejeitado** (removido/não usado — decisão de produto, não técnica):
- **`api_key` obrigatória por padrão em `/api/leitura`** (com geração automática no
  boot) — contradiz a decisão já tomada de opt-in por posto (README, §"Autenticação").
  Ativar isso hoje derrubaria a integração de todo posto sem api_key configurada.
- **`DELETE /api/usuarios/{id}`** (exclusão definitiva) — só desativação
  (`ativo=False`), reversível e preserva o registro para auditoria.
- Gate de escrita **por middleware genérico** (`_negar_por_papel`, qualquer
  POST/PUT/DELETE exige admin por padrão, salvo lista curta) — arquitetura
  interessante (uma rota nova nasce protegida sem precisar que alguém lembre de
  anotá-la) mas trocar o modelo já testado (`Depends(deps.exigir_admin)` por rota)
  por esse no meio da reconciliação era risco desnecessário. Fica registrado como
  ideia pra quando o número de rotas justificar.

`testes/unitarios/test_autenticacao.py`, `test_autorizacao.py` e `test_usuarios.py`
foram reescritos linha a linha pra testar o que ficou de pé (nenhum teste foi só
apagado por dar trabalho — cada um foi adaptado pro design real ou removido com
justificativa, registrada nos próprios arquivos).

### Recomendação de processo

O incidente só foi possível porque duas sessões desenvolveram RBAC ao mesmo tempo
sem se ver. Não tem solução técnica — é hábito de equipe:
- `git fetch origin` (ou olhar PRs abertas) antes de embarcar em algo grande que
  mexe em superfície compartilhada (auth, cadastro, schema).
- Merges menores e mais frequentes em vez de branches longas divergindo — quanto
  maior a janela, maior a chance de duas pessoas (ou duas sessões) resolverem o
  mesmo problema em paralelo sem saber.
- Depois de um merge não-trivial, rodar a suíte de testes (agora automático via CI,
  ver `.github/workflows/testes.yml`) antes de considerar o merge "resolvido".

---

## 22. Usuários e permissões — segunda leva (auditoria, autoatendimento)

Depois do RBAC (admin/operador/cliente, §21), uma rodada de melhorias sobre a MESMA
área — todas opcionais/aditivas, nada muda o comportamento de quem já usava o sistema
sem configurar nada a mais:

- **Log de auditoria** (`banco.auditoria_registrar`/`auditoria_listar`, tabela
  `auditoria`, painel em `/auditoria`, admin-only): login/login_falha, criação/edição
  de usuário, troca de senha (self, admin, ou por link), criação/edição/remoção de
  posto e entidade, gerar/revogar api_key, definir retenção, salvar configuração
  (só os NOMES das chaves alteradas, nunca o valor — várias são segredo). Deliberadamente
  **não** cobre CRUD de automação/bico/câmera — escopo cortado para não inflar demais
  esta rodada; extensível pelo mesmo padrão se algum dia fizer falta.
- **Política de senha** (`app/seguranca/sessao.py:senha_fraca`): mínimo 8 caracteres
  (já existia) + pelo menos 2 classes de caractere (letra/dígito/símbolo). Não exige
  símbolo obrigatório de propósito — NIST 800-63B desaconselha regra de complexidade
  rígida, que na prática empurra pra padrões previsíveis tipo "Senha123!". Aplicada em
  todo ponto que define senha (bootstrap, criação/edição de usuário, reset por admin,
  troca self-service, redefinição por link).
- **Cookie `Secure` configurável** (`cookie_secure`, `app/web/auth.py`): desligado por
  padrão (senão quebra o acesso local via `http://localhost`); ligar quando o servidor
  estiver atrás de proxy reverso com TLS.
- **"Esqueci minha senha"** (`/esqueci-senha` → `/redefinir-senha/{token}`) e
  **convite por e-mail** na criação de usuário (`/usuarios` → "Convidar por e-mail"):
  os dois reaproveitam o mesmo mecanismo de token de uso único (`reset_senha_tokens`,
  2h de validade). Exigem SMTP configurado (`smtp_*` em Configuração → Sistema) — sem
  isso, ambos mostram um aviso ("peça a um administrador") em vez de quebrar. Convite
  gera uma senha placeholder aleatória que ninguém conhece (nem quem criou a conta);
  a pessoa convidada define a senha de verdade pelo link.
- **Sessões ativas** (`/minha-conta` → "Sessões ativas", `banco.sessoes_listar_do_usuario`):
  qualquer usuário vê e revoga individualmente as próprias sessões — útil pra notar um
  acesso esquecido aberto em outro aparelho. Escopado ao dono: o endpoint de revogação
  confere que o token pertence a quem está pedindo antes de apagar.
- **Confirmação na UI antes de desativar ou rebaixar** um admin (`usuarios.html`) —
  só client-side (a trava de verdade já existe no backend desde §21); evita clique
  acidental.
- **Último login** na listagem de usuários (`usuarios.ultimo_login`) — mostra "Nunca"
  pra conta criada e nunca usada.

**Deliberadamente fora desta rodada** (mencionado a pedido, não implementado — sem
necessidade concreta ainda, não porque seja difícil):
- **Permissões granulares por módulo** (ex.: "pode editar câmera mas não postos") — os
  três papéis atuais (admin/operador/cliente) cobrem os casos de uso reais até aqui;
  criar um sistema de permissões bit-a-bit sem nenhum consumidor concreto é complexidade
  especulativa (mais código, mais testes, mais superfície de ataque) por um benefício
  hipotético. Se surgir um caso real ("operador X pode mexer em Y mas não Z"), vale
  revisitar.
- **Tokens de API pessoais** (um token por usuário/operador, para scripts, distinto da
  `api_key` global) — mesma lógica: ninguém pediu ainda um script/integração rodando
  como um usuário específico. A `api_key` global mais o `X-API-Key` já cobre o caso de
  integração hoje existente (o roteador do posto).
