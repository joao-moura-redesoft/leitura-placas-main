# Leitura de Placas — ALPR multi-tenant

Servidor **central único** de reconhecimento automático de placas (ALPR), que atende
todos os clientes/postos a partir de uma instalação. Roda em Python/FastAPI, projetado
para câmeras IP Intelbras.

Modelo de operação: **reativo**. O sistema de automação de cada posto chama
`GET /api/leitura` quando um abastecimento termina; o servidor resolve entidade →
posto (CNPJ) → automação → bico, recorta a área de captura daquele bico específico
numa foto fresca da câmera, e devolve a placa. Contrato completo em
[docs/INTEGRACAO_ROTEADOR.md](docs/INTEGRACAO_ROTEADOR.md).

## Funcionalidades

- **Cadastro multi-tenant**: entidade (rede) → posto (CNPJ) → automação (desempata quando o posto tem 2 sistemas) → bico (código do GET, câmera, área própria)
- **Câmera compartilhada**: vários bicos podem usar a mesma câmera, cada um com sua própria área de captura (ROI); editor visual por câmera, um retângulo por bico
- **Leitura reativa por confiança** ("reject-retry"): tira fotos incrementalmente até o consenso entre as leituras ficar forte ou o tempo esgotar, em vez de um número fixo de fotos
- **Detecção em 2 estágios**: veículo (YOLOX-s, Apache-2.0) → placa dentro do veículo (open-image-models YOLOv9-t, **MIT**, comercialmente permissivo) — elimina falso positivo fora de veículo e melhora placa pequena/distante
- **OCR ensemble**: múltiplos engines com seleção automática por padrão de placa (Mercosul/antigo), reforçado por PaddleOCR (Apache-2.0) para placa antiga borrada; consenso por posição de caractere entre frames e engines
- **GPU-adaptivo**: detecção e OCR usam CUDA automaticamente quando disponível, caem para CPU sem erro quando não — sem mudar código entre dev e produção
- **Placas suportadas**: Mercosul carro e moto, padrão antigo carro e moto
- **Câmeras**: RTSP genérico, **Intelbras VIP** (protocolo Dahua), USB, CSI — uma conexão RTSP por vez por câmera (lock por câmera evita 2ª conexão quando bicos compartilham câmera)
- **Painel Integração**: chamadas do roteador (sucesso/falha), taxa de sucesso, acordo médio, e em qual nível do cadastro uma chamada foi recusada
- **Histórico** com posto/bico de origem, recorte da placa e quadro inteiro de cada leitura
- **Modo contínuo opcional** (pipeline por câmera, tracker IoU/ByteTrack, streaming MJPEG/HLS) — inerte por padrão; útil para diagnóstico visual, mas não é o modo de operação alvo
- **Autenticação**: login com bcrypt para o painel, com três papéis (`/usuarios`) — `admin` (vê e edita tudo), `operador` (vê todos os postos e opera o dia a dia, mas não configura o sistema nem mexe no cadastro estrutural) e `cliente` (restrito a UM posto: vê postos/câmeras/histórico/integração só dele). Qualquer usuário logado troca a própria senha em `/minha-conta`, sem depender de admin. `GET /api/leitura` continua público por padrão (rede interna); cada posto pode opcionalmente ganhar uma **api_key própria** (`/empresas` → "API/LGPD") — só aquele CNPJ passa a exigir a chave, os demais continuam públicos
- **Retenção por cliente**: prazo de apagamento de detecções/chamadas (LGPD) é global por padrão, mas cada posto pode ter um prazo próprio (`/empresas` → "API/LGPD")
- **Rate limiting** simples em memória no login (força bruta) e em `/api/leitura` (abuso/varredura de CNPJ)
- **Lista branca/negra** com alertas via webhook

## Stack

| Camada | Tecnologia |
|--------|-----------|
| Backend | FastAPI + Uvicorn |
| Detecção | open-image-models YOLOv9-t (MIT) + YOLOX-s veículo (Apache-2.0), ONNX Runtime — CUDA se disponível, senão CPU |
| OCR | AutoOCR (EasyOCR + fast-plate-ocr, seleção automática) + PaddleOCR (reforço na leitura) / Tesseract / docTR |
| Banco | SQLite com WAL mode (`placas.db`) |
| Stream | MJPEG via gerador assíncrono · HLS via FFmpeg (opcional) — modo contínuo, opcional |
| Auth | bcrypt + sessão em cookie HttpOnly (painel) · `api_key` opcional (integração) |
| Hardware-alvo | Servidor central com GPU (produção) · CPU (desenvolvimento) — adapta sozinho |

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

É só isso — não há passo manual antes nem depois. No primeiro boot o container prepara
os volumes, gera o `config.txt` a partir dos padrões do projeto e baixa o detector de
veículo (~36 MB, repositório público). Em seguida abra `http://localhost:14000`, crie o
primeiro administrador e cadastre entidade → posto → automação → câmera → bico.

O servidor roda como usuário sem privilégio (`alpr`, UID 1000); só a preparação inicial
dos volumes acontece como root, dentro do entrypoint. Confira com:

```bash
docker compose exec alpr id      # esperado: uid=1000(alpr)
```

**Backup:** só a pasta `dados/` importa — ela guarda `config.txt` e `placas.db`. É um
**diretório**, e não o banco montado como arquivo, porque o SQLite roda em modo WAL e
grava `placas.db-wal`/`placas.db-shm` ao lado do banco; montar só o arquivo deixaria
esses dois no filesystem efêmero do container e perderia escritas ao recriá-lo.

**Acesso pela rede:** por padrão a porta é publicada só em `127.0.0.1`, porque
`/api/leitura` não tem autenticação (é chamado pelo sidecar Java do posto). Para abrir na
rede interna, crie um `.env` ao lado do compose com `BIND_ADDR=0.0.0.0` — com firewall ou
VPN na frente, e de preferência um proxy reverso com TLS.

#### Servidor de produção com GPU NVIDIA

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

Requer o [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
no host (o driver basta; CUDA/cuDNN vêm na imagem). Confirme que a GPU foi realmente
usada — `app/visao/hardware.py` cai para CPU **sem erro** se não achar CUDA:

```bash
docker compose logs alpr | grep -i cuda
# esperado: "ONNX Runtime: GPU CUDA disponível — detecção acelerada por GPU"
```

> A imagem GPU (`Dockerfile.gpu`) foi escrita seguindo a matriz de compatibilidade do
> `onnxruntime-gpu` 1.25 (CUDA 12.x + cuDNN 9), mas **não pôde ser construída nem testada
> no ambiente de desenvolvimento** (Windows, sem GPU e sem Docker). Valide no servidor
> real seguindo o checklist no fim do `Dockerfile.gpu` antes de colocar em produção.

## Modelo de Detecção

O backend padrão (`detector_backend = open_image_models`) **não precisa de download
manual** — o pacote `open-image-models` baixa seu próprio modelo (YOLOv9-t, licença
**MIT**) na primeira execução. É o backend recomendado: licença permissiva para uso
comercial.

O detector de veículo do estágio 1 (`models/vehicle_detector.onnx`, YOLOX-s,
Apache-2.0) **não vem no repositório** (`models/` está no `.gitignore`) — baixe com:

```bash
python scripts/baixar_modelo.py --veiculo
```

Sem esse arquivo, a detecção em 2 estágios cai automaticamente para o modo de 1
estágio (busca a placa no frame inteiro) — funciona, só perde a vantagem de eliminar
falso positivo fora de veículo.

`scripts/baixar_modelo.py` baixa um modelo alternativo (YOLO26 fine-tunado, backend
`detector_backend = onnx`) — **atenção**: a fonte padrão desse script é
**AGPL-3.0/não-comercial** (fine-tuning no dataset UFPR-ALPR). Só use esse caminho se
a licença for compatível com o uso pretendido; para produção comercial, o backend
`open_image_models` (padrão) é o que atende essa exigência.

Sem nenhum modelo carregado, o sistema cai em fallback por contornos Canny (funciona,
mas bem menos preciso).

## Configuração

Edite via UI em `/configuracao` ou diretamente em `config.txt`. Principais parâmetros:

| Chave | Padrão | Descrição |
|-------|--------|-----------|
| `detector_backend` | `open_image_models` | `open_image_models` (MIT, padrão) ou `onnx` (modelo local, ver licença acima) |
| `veiculo_dois_estagios_get` | `sim` | Detecção em 2 estágios (veículo→placa) na leitura sob demanda |
| `ocr_engine` | `auto` | `auto`, `easyocr`, `fast_plate_ocr`, `tesseract`, `paddleocr`, `doctr` |
| `ocr_leitura_paddle` | `sim` | Reforço PaddleOCR na leitura sob demanda (ajuda placa antiga borrada) |
| `leitura_timeout_seg` | `28` | Teto de tempo do loop reject-retry por chamada |
| `leitura_acordo_minimo` | `0.80` | Concordância mínima entre leituras para parar antecipadamente |
| `salvar_frame_deteccao` | `sim` | Guarda o quadro inteiro de cada detecção, além do recorte (Histórico) |
| `deteccao_automatica` | `sim` | Ativa pipeline contínuo por câmera; câmera some do orçamento de conexão RTSP única quando ligado |
| `camera_tipo` | `intelbras` | `usb`, `rtsp`, `intelbras`, `csi` (por câmera, não mais global) |
| `webhook_url` | — | URL para POST em cada detecção (modo contínuo) |
| `tracker_ativo` | `sim` | Rastreamento IoU/ByteTrack no modo contínuo |
| `streaming_modo` | `mjpeg` | `mjpeg` ou `hls` (HLS requer FFmpeg) — modo contínuo |
| `rtsp_transporte` | `tcp` | `tcp` (estável) ou `udp` |
| `api_key` | — | Se preenchida, exige `X-API-Key` nas rotas autenticadas do PAINEL inteiro (não em `/api/leitura`, hoje público). Chave GLOBAL do servidor — diferente da api_key OPCIONAL por posto (`/empresas` → "API/LGPD"), que só afeta `/api/leitura` daquele CNPJ específico |

Lista completa de chaves em `app/core/config.py` (`PADROES`), editável via `/configuracao`.

## API REST

O contrato do endpoint que o roteador do posto chama — `GET /api/leitura` — está
documentado à parte, com todos os formatos de resposta e recomendações para quem for
desenvolver esse lado: [docs/INTEGRACAO_ROTEADOR.md](docs/INTEGRACAO_ROTEADOR.md).

A referência completa de todos os endpoints (cadastro multi-tenant, câmeras, histórico,
diagnóstico, testes), com um executor interativo, fica em `/documentacao` dentro da
própria aplicação — inclui exemplos conferidos contra a resposta real de cada rota.

Principais grupos:

| Área | Endpoints |
|------|-----------|
| Leitura reativa | `GET /api/leitura`, `GET /api/chamadas`, `GET /api/chamadas/resumo` |
| Postos (visão consolidada) | `GET /api/postos`, `GET /api/postos/{empresa_id}` |
| Cadastro | `/api/entidades`, `/api/empresas`, `/api/automacoes`, `/api/bicos` (CRUD completo) |
| Bico | `PUT`/`DELETE /api/bicos/{id}/roi`, `POST /api/bicos/{id}/ler-placa-teste` |
| Câmeras | `/api/cameras` (CRUD), `GET /api/cameras/{id}/detalhe`, `GET /api/cameras/{id}/snapshot` |
| Histórico | `GET /api/deteccoes`, `GET /api/placa/{placa}` |
| Diagnóstico | `GET /api/health`, `GET /api/stats`, `GET /api/logs` |

Documentação Swagger automática do FastAPI: `http://localhost:14000/docs`.

## Exemplo de Resposta — `GET /api/leitura`

```json
{
  "entidade": "OPCAO", "cnpj": "12345678000111", "automacao": "1", "bico": "1",
  "camera_id": 3, "bico_id": 2,
  "placa": "PGK2D93",
  "padrao": "mercosul",
  "confianca": 0.91,
  "acordo": 0.85,
  "votos_snapshot": 5, "total_snapshots": 6,
  "tentativas": 6,
  "parada_motivo": "acordo",
  "snapshot": "/static/snapshots/20260721T185912_PGK2D93.jpg",
  "frame_url": "/static/snapshots/preview_bico_2.jpg"
}
```

`"acordo"` (0 a 1) é a confiança do consenso interno entre fotos/engines — vale tratar
valores baixos (~abaixo de 0.6) como leitura duvidosa. Detalhe completo dos campos e
dos demais formatos de resposta (sem placa, cadastro não encontrado/desativado, erro
de câmera) em [docs/INTEGRACAO_ROTEADOR.md](docs/INTEGRACAO_ROTEADOR.md).

## Câmeras Intelbras

URL RTSP montada automaticamente:

```
# Padrão (maioria dos modelos VIP)
rtsp://admin:SENHA@192.168.1.100:554/cam/realmonitor?channel=1&subtype=1

# Legado (VIP 1120/1220/1130)
rtsp://192.168.1.100:554/user=admin&password=SENHA&channel=1&stream=1.sdp?
```

> **Aviso:** câmeras Intelbras aceitam uma única conexão RTSP simultânea. O sistema
> reutiliza o frame do pipeline ativo em vez de abrir segunda conexão, e um lock por
> câmera serializa a conexão direta quando isso não é possível — importante quando
> vários bicos compartilham a mesma câmera.

## Estrutura do Projeto

```
leitura-placas/
├── app/                     # Código de produção (execução: python -m app.main)
│   ├── main.py              # Ponto de entrada (argparse --reload)
│   ├── servidor.py          # FastAPI app + lifespan + aquecimento dos modelos no boot
│   ├── core/                # Infra compartilhada
│   │   ├── config.py        # Lê/grava config.txt
│   │   ├── estado.py        # Estado global compartilhado entre threads
│   │   ├── banco.py         # SQLite (cadastro multi-tenant, detecções, chamadas, listas, auth)
│   │   └── broadcaster.py   # Hub WebSocket
│   ├── visao/                # Domínio de visão computacional
│   │   ├── leitura.py       # Loop reject-retry da leitura sob demanda (reativa + teste)
│   │   ├── pipeline.py      # Loop de detecção contínua por câmera (opcional, inerte por padrão)
│   │   ├── camera.py        # Captura OpenCV (USB/CSI/RTSP/Intelbras)
│   │   ├── detector.py      # open-image-models/ONNX + detecção 2 estágios (veículo→placa)
│   │   ├── hardware.py      # Detecção de GPU/CUDA
│   │   ├── ambiente.py      # Ajuste adaptativo de imagem
│   │   ├── validador.py     # Regex + correções posicionais
│   │   ├── tracker.py       # IoU Tracker + wrapper ByteTrack (modo contínuo)
│   │   └── ocr/             # AutoOCR + engines + ensemble PaddleOCR
│   │       ├── engines.py
│   │       └── auto.py
│   ├── streaming/
│   │   ├── stream.py        # Gerador MJPEG
│   │   └── hls_encoder.py   # HLS via FFmpeg (opcional)
│   ├── operacao/
│   │   ├── supervisor.py    # WorkerSupervisor (liveness + backoff) do modo contínuo
│   │   └── dns_server.py    # DNS local embutido (opcional)
│   ├── seguranca/
│   │   └── sessao.py        # bcrypt + sessões em memória
│   └── web/                 # Rotas + assets
│       ├── api.py           # Câmeras, histórico, config, diagnóstico
│       ├── leitura.py       # GET /api/leitura (endpoint reativo)
│       ├── cadastro.py      # CRUD entidades/empresas/automacoes/bicos + postos consolidado
│       ├── auth.py          # Login, logout, criar-admin
│       ├── paginas.py       # Páginas HTML (Jinja2)
│       ├── stream.py        # Endpoints MJPEG/snapshot (modo contínuo)
│       ├── testes.py        # Dataset de acurácia OCR, captura por bico
│       ├── templates/       # HTML Jinja2
│       │   ├── base.html         # Layout base + fetch 401 interceptor
│       │   ├── postos.html       # Ponto de partida: lista de postos
│       │   ├── posto.html        # Detalhe do posto: câmeras, bicos, ao vivo, teste
│       │   ├── posto_novo.html   # Assistente de cadastro
│       │   ├── roi_camera.html   # Editor de área por câmera (um retângulo por bico)
│       │   ├── entidades.html, empresas.html, automacoes.html, bicos.html  # CRUD avulso
│       │   ├── dashboard.html    # Painel Integração
│       │   └── ...
│       └── static/          # Servido em /static (inclui snapshots/)
├── benchmarks/
│   ├── benchmark_stream.py
│   └── benchmark_tracker.py
├── scripts/
│   ├── baixar_modelo.py     # Modelo alternativo — ver aviso de licença acima
│   └── treinar_yolo26s.py
├── testes/
│   ├── dataset.json              # 42 placas (sintéticas + reais)
│   ├── run_testes.py
│   ├── medir_concorrencia.py     # Mede degradação sob leituras simultâneas (rodar em produção)
│   └── gerar_placas_sinteticas.py
├── docs/
│   ├── INTEGRACAO_ROTEADOR.md    # Contrato do GET /api/leitura para quem desenvolve o roteador
│   └── ARQUITETURA.md, CASOS_DE_USO.md, OTIMIZACAO.md, ...
├── models/                  # vehicle_detector.onnx (incluso); demais modelos baixados sob demanda
├── config.txt               # Configuração (gerado no primeiro uso)
└── placas.db                 # Banco SQLite
```

Veja [docs/ARQUITETURA.md](docs/ARQUITETURA.md) para o desenho original do pipeline de
visão computacional, e [docs/INTEGRACAO_ROTEADOR.md](docs/INTEGRACAO_ROTEADOR.md) para
o contrato da integração multi-tenant.

## Precisão OCR

Dataset de 42 placas (sintéticas + fotos reais):

| Engine | Acurácia |
|--------|---------|
| `auto` (AutoOCR) | **92.9%** (39/42) |

Limitação conhecida: caractere `Q` em posição 4 de placas Mercosul é sistematicamente lido como `O` pelos modelos EasyOCR e fast-plate-ocr (ambiguidade visual do Arial Bold em baixa resolução).
