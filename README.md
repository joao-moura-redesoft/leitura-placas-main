# Leitura de Placas — ALPR v2.1

Sistema de reconhecimento automático de placas veiculares (ALPR) para postos de combustível e estacionamentos. Roda em Python/FastAPI com YOLO26 + AutoOCR dual-engine, projetado para câmeras IP Intelbras e Raspberry Pi.

## Funcionalidades

- **Detecção com YOLO26** (ONNX, sem GPU) — multi-detector com votação por IoU
- **AutoOCR dual-engine**: EasyOCR + fast-plate-ocr selecionados automaticamente por tipo de placa
- **Placas suportadas**: Mercosul carro e moto, padrão antigo carro e moto
- **Câmeras**: USB, CSI (Raspberry Pi), RTSP genérico, **Intelbras VIP** (protocolo Dahua)
- **Multi-câmera real**: cada câmera tem pipeline, thread e stream independentes
- **Tracker IoU / ByteTrack**: reduz chamadas OCR em 80–99% rastreando veículos entre frames
- **ROI por câmera**: define área de captura visual — apenas placas dentro da área são processadas
- **Supervisor**: monitora liveness e frescor de frame; reinicia pipelines mortos com backoff exponencial
- **Streaming MJPEG ou HLS** (HLS requer FFmpeg instalado)
- **Autenticação**: login com bcrypt, sessão via cookie HttpOnly 7 dias
- **Integração posto**: resposta inclui `bomba` e `lado` para associar leitura ao abastecimento
- **Lista branca/negra** com alertas via webhook
- **Dashboard** ao vivo com saúde das câmeras, stream, bounding boxes e feed de detecções
- **API REST** completa — consulta por placa, histórico, stats, ROI, health

## Stack

| Camada | Tecnologia |
|--------|-----------|
| Backend | FastAPI + Uvicorn |
| Detecção | YOLO26n ONNX Runtime (CPUExecutionProvider) |
| OCR | EasyOCR + fast-plate-ocr (AutoOCR) / Tesseract / PaddleOCR / docTR |
| Banco | SQLite com WAL mode (`placas.db`) |
| Stream | MJPEG via gerador assíncrono · HLS via FFmpeg (opcional) |
| Auth | bcrypt + sessão em cookie HttpOnly |
| Hardware-alvo | Raspberry Pi 4/5 (4 GB+) · Windows (desenvolvimento) |

## Início Rápido

```bash
pip install -r requirements.txt
python -m app.main
```

Acesse `http://localhost:14000`. Na primeira execução, o setup wizard guia a criação do admin e a configuração inicial.

### Modo desenvolvimento (hot-reload)

```bash
python -m app.main --reload
```

Requer `watchfiles` (já incluso em `requirements.txt`).

### Docker

```bash
docker compose up -d --build
```

## Modelo de Detecção

O detector usa YOLO26n ONNX em `models/plate_detector.onnx`. Não está no repositório — baixe com:

```bash
python scripts/baixar_modelo.py          # HuggingFace (padrão)
```

Sem o modelo, o sistema cai em fallback por contornos Canny (funciona, mas menos preciso).

## Configuração

Edite via UI em `/configuracao` ou diretamente em `config.txt`. Principais parâmetros:

| Chave | Padrão | Descrição |
|-------|--------|-----------|
| `ocr_engine` | `auto` | `auto`, `easyocr`, `fast_plate_ocr`, `tesseract`, `paddleocr`, `doctr` |
| `deteccao_automatica` | `sim` | Ativa pipeline contínuo; `nao` = apenas leitura manual |
| `camera_tipo` | `intelbras` | `usb`, `rtsp`, `intelbras`, `csi` |
| `intelbras_host` | — | IP da câmera Intelbras |
| `salvar_snapshot` | `nao` | Salva JPEG do crop em `static/snapshots/` |
| `webhook_url` | — | URL para POST em cada detecção |
| `cooldown_seg` | `30` | Tempo mínimo entre detecções da mesma placa |
| `frames_consenso` | `3` | Leituras iguais consecutivas para confirmar placa |
| `tracker_ativo` | `sim` | Ativa rastreamento IoU/ByteTrack (reduz OCR em 80–99%) |
| `tracker_ocr_intervalo` | `5` | Frames entre leituras OCR do mesmo veículo |
| `tracker_votos_emitir` | `2` | Votos necessários para emitir placa pelo tracker |
| `streaming_modo` | `mjpeg` | `mjpeg` ou `hls` (HLS requer FFmpeg) |
| `rtsp_transporte` | `tcp` | `tcp` (estável) ou `udp` |
| `snapshots_votacao` | `3` | Frames por leitura manual (votação maioria) |

## API REST — Endpoints Principais

| Método | Rota | Descrição |
|--------|------|-----------|
| `GET` | `/api/placa/{placa}` | Consulta consolidada: última detecção, lista branca/negra, histórico |
| `POST` | `/api/cameras/{id}/ler-placa` | Captura frame e lê placa (retorna `bomba`, `lado`, `placa`, confiança) |
| `GET` | `/api/cameras/{id}/snapshot` | Frame atual da câmera como JPEG |
| `PUT` | `/api/cameras/{id}/roi` | Salva área de captura `{x, y, w, h}` e aplica imediatamente |
| `DELETE` | `/api/cameras/{id}/roi` | Remove área (volta a monitorar frame completo) |
| `GET` | `/api/health` | Saúde por câmera: status, restarts, backoff, última detecção |
| `GET` | `/api/deteccoes` | Histórico paginado com filtros |
| `GET` | `/api/stats` | Totais, top 10 placas, fps, uptime, modo de streaming |
| `GET` | `/api/cameras` | Câmeras cadastradas |
| `POST` | `/api/cameras` | Cadastra câmera e inicia pipeline |
| `POST` | `/api/cameras/scan` | Varre sub-rede em busca de câmeras com RTSP |
| `GET` | `/api/listas` | Lista branca/negra |
| `POST` | `/api/listas` | Cadastra placa na lista |
| `GET` | `/api/status` | Estado do pipeline em tempo real |

Documentação interativa: `http://localhost:14000/docs` (Swagger UI automático do FastAPI).

## Exemplo de Resposta — `POST /api/cameras/1/ler-placa`

```json
{
  "bomba": 1,
  "lado": 2,
  "placa": "ABC1D23",
  "padrao": "mercosul",
  "confianca": 0.87,
  "votos_snapshot": 2,
  "total_snapshots": 3,
  "votos_ocr": 1,
  "total_engines": 2,
  "detalhes_ocr": [
    {"engine": "easyocr",       "placa": "ABC1D23", "padrao": "mercosul", "confianca": 0.97},
    {"engine": "fast_plate_ocr","placa": "ABC1D23", "padrao": "mercosul", "confianca": 0.88}
  ],
  "snapshot": null,
  "frame_url": "/static/snapshots/preview_1.jpg"
}
```

## Câmeras Intelbras

URL RTSP montada automaticamente:

```
# Padrão (maioria dos modelos VIP)
rtsp://admin:SENHA@192.168.1.100:554/cam/realmonitor?channel=1&subtype=1

# Legado (VIP 1120/1220/1130)
rtsp://192.168.1.100:554/user=admin&password=SENHA&channel=1&stream=1.sdp?
```

> **Aviso:** câmeras Intelbras aceitam uma única conexão RTSP simultânea. O sistema reutiliza o frame do pipeline ativo em vez de abrir segunda conexão.

## Estrutura do Projeto

```
leitura-placas/
├── app/                     # Código de produção (execução: python -m app.main)
│   ├── main.py              # Ponto de entrada (argparse --reload)
│   ├── servidor.py          # FastAPI app + lifespan + HLS mount
│   ├── core/                # Infra compartilhada
│   │   ├── config.py        # Lê/grava config.txt
│   │   ├── estado.py        # Estado global compartilhado entre threads
│   │   ├── banco.py         # SQLite (detecções, listas, câmeras, auth)
│   │   └── broadcaster.py   # Hub WebSocket
│   ├── visao/                # Pipeline de visão computacional
│   │   ├── pipeline.py      # Loop de detecção por câmera (thread)
│   │   ├── camera.py        # Captura OpenCV (USB/CSI/RTSP/Intelbras)
│   │   ├── detector.py      # YOLO/open-image-models ONNX + fallback contornos
│   │   ├── hardware.py      # Detecção de GPU/CUDA
│   │   ├── ambiente.py      # Ajuste adaptativo de imagem
│   │   ├── validador.py     # Regex + correções posicionais
│   │   ├── tracker.py       # IoU Tracker + wrapper ByteTrack
│   │   └── ocr/             # AutoOCR + 5 engines + pré-processamento
│   │       ├── engines.py
│   │       └── auto.py
│   ├── streaming/
│   │   ├── stream.py        # Gerador MJPEG
│   │   └── hls_encoder.py   # HLS via FFmpeg (opcional)
│   ├── operacao/
│   │   ├── supervisor.py    # WorkerSupervisor (liveness + backoff)
│   │   └── dns_server.py    # DNS local embutido (opcional)
│   ├── seguranca/
│   │   └── sessao.py        # bcrypt + sessões em memória
│   └── web/                 # Rotas + assets
│       ├── api.py           # Endpoints REST
│       ├── auth.py          # Login, logout, criar-admin
│       ├── paginas.py       # Páginas HTML (Jinja2)
│       ├── stream.py        # Endpoints MJPEG/snapshot
│       ├── testes.py        # Avaliação de acurácia OCR via UI
│       ├── templates/       # HTML Jinja2
│       │   ├── base.html    # Layout base + fetch 401 interceptor
│       │   ├── login.html
│       │   ├── cameras.html
│       │   ├── roi.html     # Editor visual de área de captura
│       │   └── ...
│       └── static/          # Servido em /static (inclui snapshots/)
├── benchmarks/
│   ├── benchmark_stream.py
│   └── benchmark_tracker.py
├── scripts/
│   ├── baixar_modelo.py
│   └── treinar_yolo26s.py
├── testes/
│   ├── dataset.json         # 42 placas (sintéticas + reais)
│   ├── run_testes.py
│   └── gerar_placas_sinteticas.py
├── docs/                    # ARQUITETURA.md, CASOS_DE_USO.md, OTIMIZACAO.md, ...
├── models/                  # Modelos ONNX (baixados via scripts/baixar_modelo.py)
├── config.txt               # Configuração (gerado no primeiro uso)
└── placas.db                 # Banco SQLite
```

Veja [docs/ARQUITETURA.md](docs/ARQUITETURA.md) para detalhamento completo de módulos, fluxo de dados e endpoints.

## Precisão OCR

Dataset de 42 placas (sintéticas + fotos reais):

| Engine | Acurácia |
|--------|---------|
| `auto` (AutoOCR) | **92.9%** (39/42) |

Limitação conhecida: caractere `Q` em posição 4 de placas Mercosul é sistematicamente lido como `O` pelos modelos EasyOCR e fast-plate-ocr (ambiguidade visual do Arial Bold em baixa resolução).
