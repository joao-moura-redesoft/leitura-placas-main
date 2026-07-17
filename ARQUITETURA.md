# Arquitetura do Sistema ALPR

> Estado atual · maio/2026 · commit `c58e85d` · sincronizado com o código.

---

## 1. Visão Geral

Sistema de reconhecimento automático de placas (ALPR) rodando em Python com FastAPI, YOLO26n ONNX e suporte a múltiplos engines OCR. Desenvolvido no Windows, direcionado também para produção em Raspberry Pi ARM64.

```
┌──────────────────────────────────────────────────────────────────┐
│                          servidor.py                              │
│                FastAPI + Uvicorn (porta 14000)                    │
│  ┌──────────┐  ┌────────────┐  ┌────────────┐  ┌─────────────┐  │
│  │  paginas │  │   auth     │  │   stream   │  │  api (REST) │  │
│  └──────────┘  └────────────┘  └────────────┘  └─────────────┘  │
└─────────────────────────┬────────────────────────────────────────┘
                           │ lê / escreve
                    ┌──────▼──────┐
                    │  estado.py  │  ← fps, logs, detecções recentes,
                    │  (memória   │     frames por câmera, crop OCR
                    │ compartilhada)
                    └──────┬──────┘
                           │
      ┌────────────────────▼──────────────────────────────────────┐
      │                   pipeline.py                              │
      │  _instancias: dict[int, Pipeline]  (1 por câmera)         │
      │                                                            │
      │  Pipeline A  Pipeline B  Pipeline C  ...                   │
      │  (cam id=1)  (cam id=2)  (cam id=3)                       │
      │     │            │           │                             │
      │  Camera       Camera      Camera                           │
      │  ROI crop     ROI crop    ROI crop   ← área configurável   │
      │  Detector     Detector    Detector                         │
      │  Tracker      Tracker     Tracker    ← IoU / ByteTrack     │
      │  OCR          OCR         OCR                              │
      │  Validador    Validador   Validador                        │
      │  Banco        Banco       Banco                            │
      └────────────────────────────────────────────────────────────┘
                           │ supervisiona
                    ┌──────▼──────────┐
                    │  supervisor.py  │  ← liveness + frame freshness
                    │  (WorkerSupervisor) backoff exponencial      │
                    └─────────────────┘

         hls_encoder.py (opcional)
         FFmpeg por câmera → hls/{id}/*.ts + index.m3u8
```

Cada câmera cadastrada no banco recebe sua própria instância de `Pipeline` rodando em thread dedicada. O `WorkerSupervisor` monitora liveness e frescor de frame, reiniciando pipelines mortos com backoff exponencial (5 s → 300 s). As instâncias são gerenciadas pelo dicionário `_instancias: dict[int, Pipeline]` em `pipeline.py`.

---

## 2. Módulos e Responsabilidades

| Arquivo | Responsabilidade |
|---------|-----------------|
| `main.py` | Ponto de entrada — argparse (`--reload`), suprime warnings PyTorch, delega para `servidor.iniciar()` |
| `servidor.py` | Cria app FastAPI, lifespan, monta rotas, monta `/hls` como static, registra MIME types, verifica porta antes de subir, exibe banner |
| `config.py` | Lê/grava `config.txt` (chave=valor) + override via env vars; padrão `intelbras` |
| `estado.py` | Estado global compartilhado entre threads (lock único); ring buffer de logs; `frames_cameras: dict[int, frame]` |
| `banco.py` | Camada SQLite — detecções, listas, câmeras, usuários, sessões; WAL mode; migração incremental |
| `camera.py` | OpenCV VideoCapture: USB (CAP_DSHOW/CAP_V4L2), CSI, RTSP, Intelbras; auto-detecção backend no Windows |
| `detector.py` | YOLO26n ONNX Runtime (CPUExecutionProvider); auto-detecta formato de saída; fallback por contornos Canny |
| `ocr.py` | Tesseract, EasyOCR, PaddleOCR, docTR, fast-plate-ocr; pré-processamento multicamada |
| `validador.py` | Regex + correções posicionais (O↔0, I↔1, T↔7, B↔8…); janela deslizante; `formato_hint` |
| `pipeline.py` | Loop principal por câmera; ROI crop; rate-limit; detecção; tracker; consenso; cooldown |
| `tracker.py` | IoU Tracker interno + wrapper ByteTrack (boxmot); voto por track; reduz OCR em ~80–99% |
| `supervisor.py` | WorkerSupervisor — monitora liveness de threads e frescor de frame; reinicia com backoff exponencial |
| `hls_encoder.py` | HLSManager — subprocess FFmpeg por câmera; codifica uma vez para N viewers; segmentos `.ts` + `.m3u8` |
| `stream.py` | Gerador MJPEG global e por câmera; snapshot JPEG |
| `rotas/api.py` | API REST completa: detecções, stats, health, listas, config, câmeras, ROI, debug crop |
| `rotas/auth.py` | Autenticação: login, logout, criar-admin; sessão via cookie HttpOnly 7 dias |
| `rotas/paginas.py` | Páginas HTML via Jinja2 (inclui `/roi/{camera_id}`) |
| `rotas/stream.py` | Endpoints `/stream.mjpg`, `/stream/{id}.mjpg`, `/snapshot.jpg` |

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
   Tracker.precisa_ocr(track_id)
   ├─ False → pula OCR (veículo já identificado)
   └─ True  → OCR + Tracker.registrar_ocr(track_id, placa, ...)
                   ↓
              Tracker.placa_pronta(track_id)
              ├─ None → aguarda mais votos
              └─ (placa, padrao, conf) → _tentar_emitir
```

**Impacto medido (benchmark):** redução de OCR de 80–99% dependendo de cooldown e FPS.

**Configuração:** `tracker_ativo = sim`, `tracker_ocr_intervalo = 5`, `tracker_votos_emitir = 2`

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
- **Pré-processamento OCR em cadeia** — header removal → mascaramento QR/BR → foco nos caracteres.
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
main
 └─ servidor
     ├─ config ◄────────────────── (sem deps internas)
     ├─ banco
     ├─ estado ◄─────────────────── (sem deps internas)
     ├─ pipeline
     │   ├─ camera
     │   ├─ detector
     │   ├─ tracker ──► (boxmot opcional)
     │   ├─ ocr ──► estado (registrar_crop_ocr, registrar_frame_camera)
     │   ├─ validador ◄──────────── (sem deps internas)
     │   └─ banco
     ├─ supervisor ──► pipeline, estado
     ├─ hls_encoder ──► estado (frames_cameras)
     ├─ rotas/api ──► banco, camera, config, estado, pipeline, supervisor
     ├─ rotas/auth ──► banco
     ├─ rotas/paginas
     └─ rotas/stream ──► stream ──► estado
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
| Arquivos Python principais | 16 |
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
