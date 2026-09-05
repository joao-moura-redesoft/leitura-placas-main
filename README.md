# Leitura de Placas: ALPR multi-tenant

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
- **Detecção em 2 estágios**: veículo (YOLOX-s, Apache-2.0) → placa dentro do veículo (open-image-models YOLOv9-t, **MIT**, comercialmente permissivo), que elimina falso positivo fora de veículo e melhora placa pequena/distante
- **OCR ensemble**: múltiplos engines com seleção automática por padrão de placa (Mercosul/antigo), reforçado por PaddleOCR (Apache-2.0) para placa antiga borrada; consenso por posição de caractere entre frames e engines
- **GPU-adaptivo**: detecção e OCR usam CUDA automaticamente quando disponível, caem para CPU sem erro quando não, sem mudar código entre dev e produção
- **Placas suportadas**: Mercosul carro e moto, padrão antigo carro e moto
- **Câmeras**: RTSP genérico, **Intelbras VIP** (protocolo Dahua), USB, CSI. Uma conexão RTSP por vez por câmera (lock por câmera evita 2ª conexão quando bicos compartilham câmera)
- **Dados do veículo por placa** (apiplacas.com.br): a resposta do roteador ganha o **tipo de combustível**, marca/modelo/ano/cor e espécie do veículo lido. Cada consulta custa crédito pré-pago, então a resposta é guardada no banco e a mesma placa só é paga uma vez, porque o cache é compartilhado por todos os postos do servidor. Desligado por padrão; com a API fora, sem saldo ou lenta, a leitura continua funcionando normalmente
- **Painel Integração**: chamadas do roteador (sucesso/falha), taxa de sucesso, acordo médio, e em qual nível do cadastro uma chamada foi recusada
- **Histórico** com posto/bico de origem, recorte da placa e quadro inteiro de cada leitura
- **Modo contínuo opcional** (pipeline por câmera, tracker IoU/ByteTrack, streaming MJPEG/HLS), inerte por padrão; útil para diagnóstico visual, mas não é o modo de operação alvo
- **Autenticação**: login com bcrypt para o painel, com três papéis (`/usuarios`): `admin` (vê e edita tudo), `operador` (vê todos os postos e opera o dia a dia, mas não configura o sistema nem mexe no cadastro estrutural) e `cliente` (restrito a UM posto: vê postos/câmeras/histórico/integração só dele). Qualquer usuário logado troca a própria senha e gerencia as próprias sessões ativas em `/minha-conta`, sem depender de admin; "esqueci minha senha" (`/esqueci-senha`) e convite de usuário por e-mail (requer SMTP configurado em Configuração → Sistema) reaproveitam o mesmo link de uso único. `GET /api/leitura` continua público por padrão (rede interna); cada posto pode opcionalmente ganhar uma **api_key própria** (`/empresas` → "API/LGPD"): só aquele CNPJ passa a exigir a chave, os demais continuam públicos
- **Auditoria** (`/auditoria`, admin): quem fez o quê, incluindo login, gestão de usuários, cadastro estrutural, api_key/retenção por posto, configuração do sistema
- **Retenção por cliente**: prazo de apagamento de detecções/chamadas (LGPD) é global por padrão, mas cada posto pode ter um prazo próprio (`/empresas` → "API/LGPD")
- **Rate limiting** simples em memória no login (força bruta) e em `/api/leitura` (abuso/varredura de CNPJ)
- **Lista branca/negra** com alertas via webhook

## Stack

| Camada | Tecnologia |
|--------|-----------|
| Backend | FastAPI + Uvicorn |
| Detecção | open-image-models YOLOv9-t (MIT) + YOLOX-s veículo (Apache-2.0), ONNX Runtime (CUDA se disponível, senão CPU) |
| OCR | AutoOCR (EasyOCR + fast-plate-ocr, seleção automática) + PaddleOCR (reforço na leitura) / Tesseract / docTR |
| Banco | SQLite com WAL mode (`placas.db`) |
| Stream | MJPEG via gerador assíncrono · HLS via FFmpeg (opcional), no modo contínuo |
| Auth | bcrypt + sessão em cookie HttpOnly (painel) · `api_key` opcional (integração) |
| Hardware-alvo | Servidor central com GPU (produção) · CPU (desenvolvimento), adapta sozinho |

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

É só isso: não há passo manual antes nem depois. No primeiro boot o container prepara
os volumes, gera o `config.txt` a partir dos padrões do projeto e baixa o detector de
veículo (~36 MB, repositório público). Em seguida abra `http://localhost:14000`, crie o
primeiro administrador e cadastre entidade → posto → automação → câmera → bico.

O servidor roda como usuário sem privilégio (`alpr`, UID 1000); só a preparação inicial
dos volumes acontece como root, dentro do entrypoint. Confira com:

```bash
docker compose exec alpr id      # esperado: uid=1000(alpr)
```

**Backup:** só a pasta `dados/` importa, porque ela guarda `config.txt` e `placas.db`. É um
**diretório**, e não o banco montado como arquivo, porque o SQLite roda em modo WAL e
grava `placas.db-wal`/`placas.db-shm` ao lado do banco; montar só o arquivo deixaria
esses dois no filesystem efêmero do container e perderia escritas ao recriá-lo.

**Acesso pela rede:** por padrão a porta é publicada só em `127.0.0.1`, porque
`/api/leitura` não tem autenticação (é chamado pelo sidecar Java do posto). Para abrir na
rede interna, crie um `.env` ao lado do compose com `BIND_ADDR=0.0.0.0`, com firewall ou
VPN na frente, e de preferência um proxy reverso com TLS.

#### Servidor de produção com GPU NVIDIA

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

Requer o [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
no host (o driver basta; CUDA/cuDNN vêm na imagem). Confirme que a GPU foi realmente
usada, porque `app/visao/hardware.py` cai para CPU **sem erro** se não achar CUDA:

```bash
docker compose logs alpr | grep -i cuda
# esperado: "ONNX Runtime: GPU CUDA disponível — detecção acelerada por GPU"
```

> A imagem GPU (`Dockerfile.gpu`) foi escrita seguindo a matriz de compatibilidade do
> `onnxruntime-gpu` 1.25 (CUDA 12.x + cuDNN 9), mas **não pôde ser construída nem testada
> no ambiente de desenvolvimento** (Windows, sem GPU e sem Docker). Valide no servidor
> real seguindo o checklist no fim do `Dockerfile.gpu` antes de colocar em produção.

#### Teto de CPU e memória (não consumir a máquina toda)

O compose limita o container por `deploy.resources.limits`. Os valores vêm de medição
neste projeto (RSS do processo com os modelos já carregados), não de estimativa:

| Componente carregado no processo | RSS acumulado |
|---|---|
| python + cv2 + onnxruntime | 55 MB |
| + YOLOX veículo (2 estágios) | 146 MB |
| + detector de placa 608 | 178 MB |
| + PaddleOCR (`ocr_leitura_paddle=sim`) | **594 MB** (383 MB é só o Paddle) |

Padrão: **`cpus: 2.0`** e **`memory: 2g`** (CPU); `4.0`/`4g` no override de GPU. Para
mudar sem editar arquivo versionado, use o `.env` ao lado do compose:

```bash
ALPR_CPUS=1.5
ALPR_MEM=1500m
```

Duas armadilhas ao apertar esses números:

- **Memória abaixo de ~1g**: o container leva OOMKill durante o boot (todos os modelos
  são carregados juntos por `_aquecer_modelos_bg`), *antes* de servir a primeira leitura,
  e o `restart: unless-stopped` transforma isso num laço de reinício. Se ligar
  `ocr_leitura_easyocr=sim`, o torch soma ~700 MB: suba para `3g` **antes**.
- **`OMP_NUM_THREADS=1` não governa o onnxruntime.** Ele tem pool próprio e dimensiona
  pelo total de núcleos do *host*, inclusive os que o `cpus:` não deixa usar, e aí o
  cgroup só aplica throttle. Quem controla isso é `ONNX_INTRA_THREADS` (default `1`),
  aplicado em `app/visao/hardware.py:onnx_session_options`. Medido aqui (4 núcleos,
  YOLOX-s 640x640, com `OMP_NUM_THREADS=1` já ativo):

  | Cenário | Sem `ONNX_INTRA_THREADS` | Com `=1` |
  |---|---|---|
  | 1 sessão | 470 ms/infer, 2,19 núcleos | 428 ms/infer, 0,87 núcleo |
  | 2 câmeras | 2,31 infer/s, 2,84 núcleos | **3,66 infer/s, 1,53 núcleo** |

  Com as duas câmeras do posto o limite explícito é **58% mais rápido gastando metade da
  CPU**: o default abre dois pools de 4 threads em 4 núcleos e o tempo vai para troca de
  contexto. Não é troca de precisão por velocidade: é a mesma inferência. Subir para `2`
  só compensa em host com núcleo sobrando, medindo.

O orçamento por quadro é folgado: com `deteccao_fps_max = 1` são 1000 ms por câmera,
contra ~490 ms de inferência medidos.

#### Host compartilhado (PDV, Java, React na mesma máquina)

Quando o ALPR divide o servidor com outros serviços, **o teto de CPU não é o que faz ele
ceder**. `cpus:` vale sempre, inclusive com a máquina ociosa — apertar ali só deixa a
leitura lenta de graça de madrugada. Quem resolve disputa é `cpu_shares`:

```yaml
deploy:
  resources:
    limits:
      cpus: "2.0"      # teto: nunca passa disso, mesmo sozinho na máquina
      memory: 2g
cpu_shares: 512        # peso: só entra em ação quando há disputa (default 1024)
```

Com `512` (metade do peso normal), a máquina livre deixa o ALPR usar os 2.0 inteiros; com
o PDV pedindo CPU ao mesmo tempo, quem perde a fatia primeiro é o ALPR. Suba para `1024`
(empate) ou `2048` (ALPR prioritário) se a leitura de placa virar o gargalo do negócio.

**RAM não é a restrição.** Das 32 GB do host o ALPR usa ~0,6 GB de fato (medido). Subir o
`memory` além de `2g` não reserva nada para ele — só permite um vazamento crescer mais
antes de o Docker cortar.

**Três subsistemas dimensionam pelo host, não pela cota do container** — e os três já
estão limitados, porque `cpus: 2.0` sem isso só gera throttle do cgroup e troca de
contexto, não economia:

| Subsistema | Variável | Default | Sem o limite |
|---|---|---|---|
| onnxruntime (detecção) | `ONNX_INTRA_THREADS` | `1` | ~1,5 thread por núcleo do host |
| OpenCV (pré-processamento) | `OPENCV_NUM_THREADS` | `0` (=1) | pool interno por núcleo |
| x264 (preview HLS) | `HLS_X264_THREADS` | `2` | ~1,5x núcleos do host |

Num host de 16 núcleos, o x264 sem `-threads` abriria ~24 threads dentro de uma cota de
2 CPUs — o mesmo padrão de thrashing já medido no onnxruntime.

## Modelo de Detecção

O backend padrão (`detector_backend = open_image_models`) **não precisa de download
manual**: o pacote `open-image-models` baixa seu próprio modelo (YOLOv9-t, licença
**MIT**) na primeira execução. É o backend recomendado: licença permissiva para uso
comercial.

O detector de veículo do estágio 1 (`models/vehicle_detector.onnx`, YOLOX-s,
Apache-2.0) **não vem no repositório** (`models/` está no `.gitignore`), então baixe com:

```bash
python scripts/baixar_modelo.py --veiculo
```

Sem esse arquivo, a detecção em 2 estágios cai automaticamente para o modo de 1
estágio (busca a placa no frame inteiro). Funciona, só perde a vantagem de eliminar
falso positivo fora de veículo.

`scripts/baixar_modelo.py` baixa um modelo alternativo (YOLO26 fine-tunado, backend
`detector_backend = onnx`). **Atenção**: a fonte padrão desse script é
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
| `tiles_fallback_get` | `sim` | Quando nada é detectado, revarre o recorte em janelas sobrepostas, que é o que recupera placa de **moto** parada na bomba (ver [Placa de moto](#placa-de-moto-o-caso-difícil)) |
| `tiles_lado_alvo` | `300` | Lado alvo da janela, em px do recorte analisado |
| `tiles_sobreposicao` | `0.30` | Sobreposição entre janelas. Faixa útil estreita (de 0.25 a 0.35): mais que isso e a janela volta a ser o recorte inteiro, que é o enquadramento que falha |
| `tiles_conf` | `0.15` | Limiar de confiança só nas janelas (mais permissivo de propósito) |
| `ocr_engine` | `auto` | `auto` = ensemble: todos os engines leem e a fusão por caractere decide. Também `easyocr`, `fast_plate_ocr`, `tesseract`, `paddleocr`, `doctr` |
| `ocr_fast_modelos` | — | Membros do ensemble fast-plate-ocr (vírgula). Vazio = os três do default. Medido: 1 modelo dá carro 13/26, os três dão 17/26 |
| `ocr_leitura_paddle` | `sim` | PaddleOCR como voto a mais na leitura sob demanda: +1 carro em 26, ao custo de 747 ms por recorte |
| `ocr_leitura_easyocr` | `nao` | EasyOCR no pool. Desligada por medição: contribui zero para a fusão e acerta 1/26 sozinha, custando 212 ms. Ligue para remedir no posto |
| `acordo_metrica` | `string` | Como `acordo` é medido: `string` (fração das leituras idênticas à emitida) ou `caractere` (concordância média por posição). Trocar exige recalibrar `leitura_acordo_minimo` |
| `leitura_timeout_seg` | `28` | Teto de tempo do loop reject-retry por chamada |
| `leitura_acordo_minimo` | `0.80` | Concordância mínima entre leituras para parar antecipadamente |
| `salvar_frame_deteccao` | `sim` | Guarda o quadro inteiro de cada detecção, além do recorte (Histórico) |
| `deteccao_automatica` | `sim` | Ativa pipeline contínuo por câmera; câmera some do orçamento de conexão RTSP única quando ligado |
| `camera_tipo` | `intelbras` | `usb`, `rtsp`, `intelbras`, `csi` (por câmera, não mais global) |
| `webhook_url` | — | URL para POST em cada detecção (modo contínuo) |
| `tracker_ativo` | `sim` | Rastreamento IoU/ByteTrack no modo contínuo |
| `streaming_modo` | `mjpeg` | `mjpeg` ou `hls` (HLS requer FFmpeg), no modo contínuo |
| `rtsp_transporte` | `tcp` | `tcp` (estável) ou `udp` |
| `api_key` | — | Se preenchida, exige `X-API-Key` nas rotas autenticadas do PAINEL inteiro (não em `/api/leitura`, hoje público). Chave GLOBAL do servidor, diferente da api_key OPCIONAL por posto (`/empresas` → "API/LGPD"), que só afeta `/api/leitura` daquele CNPJ específico |
| `apiplacas_ativo` | `nao` | Liga a consulta de dados do veículo. **Desligado por padrão porque cada consulta custa crédito pré-pago** |
| `apiplacas_token` | — | Token da conta na apiplacas.com.br. Mascarado na API do painel |
| `apiplacas_modo` | `manual` | Quem dispara a consulta paga. `manual` = nada consulta sozinho, você gasta pelo botão no Histórico; `automatico` = a primeira leitura de cada placa consulta. Use `manual` enquanto a cota for curta |
| `apiplacas_ttl_dias` | `180` | Dias até reconsultar a mesma placa. Combustível/marca/modelo não mudam; `situacao` e município, sim. `0` = nunca reconsultar |
| `apiplacas_ttl_negativo_dias` | `30` | Dias até reconsultar placa que a base não conhece. A causa comum não é veículo novo, é leitura de OCR errada |
| `apiplacas_exigir_confirmada` | `sim` | Não gasta consulta em leitura sem consenso, que pode ser a placa errada. Mesmo assim ela ainda mostra o dado se já estiver em cache |
| `apiplacas_max_por_dia` | `500` | Teto de gasto diário, contado no banco (sobrevive a reinício). `0` = sem teto |
| `log_arquivo` | `alpr.log` | Log em arquivo com rotação, mais o dump do `faulthandler` em `alpr-nativo.log`. Vazio = só stderr. **Sem isto, "caiu do nada" não tem o que investigar:** `/api/logs` lê um buffer em memória que morre com o processo, e queda nativa não deixa traceback Python |
| `log_arquivo_mb` / `log_arquivo_backups` | `10` / `3` | Teto de 30 MB no disco do posto |

Lista completa de chaves em `app/core/config.py` (`PADROES`), editável via `/configuracao`.

### Estabilidade de bibliotecas nativas

O processo carrega OpenCV, onnxruntime e (se ligados) PyTorch via EasyOCR e Paddle. Cada um
traz o seu runtime de OpenMP, e no Windows o conflito produz falta nativa na carga de DLL.
Medido no log de 24/08/2026, num único processo: **1030** `Unknown C++ exception from
OpenCV code`, **2061** dumps de `Windows fatal exception` e uma `access violation` dentro de
um `import`, contra 11.273 inferências bem-sucedidas. As falhas ocupavam a janela de
3,5 min em que os modelos carregavam **em paralelo** com o pipeline de câmera já rodando
CLAHE e inferência; nela o detector de veículo estava morto e `deteccoes.tipo_veiculo`
chegou nulo ao banco, indistinguível de "não havia veículo".

Três defesas, aplicadas em 25/08/2026:

1. `app/core/nativo.py` desliga o threading interno do OpenCV e o OpenCL no boot do
   servidor, o mesmo ajuste que `testes/unitarios/conftest.py` já fazia só na suíte.
   **Custo medido** em quadro 1280x720, no `AjustadorAmbiente` completo, com as duas
   câmeras em paralelo num host de 4 núcleos: 41,7 ms/rodada com 4 threads contra 57,2 ms
   com o ajuste. Desligar o OpenCL é gratuito, porque o custo todo é da redução de threads. Com
   `deteccao_fps_max = 1` o orçamento é de 1000 ms/quadro, então isso é **5,7%** dele.
   `OPENCV_NUM_THREADS` fica disponível para o dia em que o FPS de detecção subir muito.
2. O `lifespan` **sequencia** aquecer-modelos e subir-pipeline, em vez de paralelizar.
3. Variáveis de ambiente de OpenMP, no `entrypoint.sh` e no `docker-compose.yml`
   (`OMP_NUM_THREADS=1`, `MKL_NUM_THREADS=1`, `KMP_DUPLICATE_LIB_OK=TRUE`). São variáveis e
   não código porque precisam existir antes de o runtime nativo carregar. Quem roda fora do
   Docker deve exportá-las.

Falha repetida de inferência agora dá **uma** linha de `ERROR` ("INOPERANTE") depois de 10
falhas seguidas, e outra quando recupera. Antes eram 849 WARNINGs idênticos que ninguém
leria.

## API REST

Guia completo para **outro sistema se acoplar ao nosso**: autenticação, o que enviar, o
que volta em cada desfecho, consulta de histórico, webhook/WebSocket e checklist de
integração: [docs/API_INTEGRACAO.md](docs/API_INTEGRACAO.md). É o documento para entregar
a quem vai desenvolver o outro lado.

O contrato do endpoint que o roteador do posto chama (`GET /api/leitura`) está
documentado à parte, com todos os formatos de resposta e recomendações para quem for
desenvolver esse lado: [docs/INTEGRACAO_ROTEADOR.md](docs/INTEGRACAO_ROTEADOR.md).

A referência completa de todos os endpoints (cadastro multi-tenant, câmeras, histórico,
diagnóstico, testes), com um executor interativo, fica em `/documentacao` dentro da
própria aplicação, e inclui exemplos conferidos contra a resposta real de cada rota.

### Custo da consulta de dados do veículo

O enriquecimento com dados do veículo usa a apiplacas.com.br, que cobra **por consulta**,
em crédito pré-pago. O desenho todo gira em torno de pagar **uma vez por placa**:

- a resposta é guardada na tabela `veiculos` e reaproveitada por **todos os postos** do
  servidor, ou seja, a placa que um posto pagou já vem de graça no outro;
- a resposta **inteira** é guardada, não só os campos que exibimos hoje: expor um campo
  novo amanhã não custa uma reconsulta do histórico;
- leitura sem consenso (`confirmada: false`) não gasta, porque pode ser a placa errada;
- o botão "Testar como o roteador" e a tela de detalhe da placa **nunca** consultam a API:
  mostram só o que já está em cache;
- há teto por minuto e por dia, e o sistema para de tentar sozinho quando o provedor
  responde "sem saldo" ou "token inválido" (insistir nesses dois não pode dar certo).

Com `apiplacas_modo=manual` (o padrão), **nada consulta sozinho**, nem o abastecimento. O
combustível aparece no Histórico para as placas já consultadas, e a consulta é disparada por
você: por placa, ou em lote nas placas mais vistas que ainda não têm dados, sempre mostrando
quantos créditos vai gastar antes de confirmar. Com volume contratado, troque para
`automatico` e o enriquecimento passa a acontecer no próprio abastecimento.

Saldo e uso ficam em `/configuracao` → Sistema → "Dados do veículo (API Placas)". Quando o
crédito acaba, **a leitura continua funcionando**: o payload sai com `veiculo.consulta`
igual a `"indisponivel"` e o motivo aparece no painel Integração.

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

## Exemplo de Resposta do `GET /api/leitura`

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
  "frame_url": "/api/bicos/2/preview.jpg"
}
```

`"acordo"` (0 a 1) é a confiança do consenso interno entre fotos/engines, e vale tratar
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
> câmera serializa a conexão direta quando isso não é possível, o que importa quando
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
│   ├── integracoes/
│   │   └── apiplacas.py     # Consulta paga de dados do veículo + cache na tabela `veiculos`
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

Medido em 25/08/2026 com `python testes/run_testes.py` sobre as **29 fotos reais** de
`testes/dataset.json` (as sintéticas saíram no commit `d49a78f`, porque elas invertiam o sinal
da medição, ver "Placa de moto"):

| `ocr_engine=auto` | antes | depois |
|---|---|---|
| total | 12/29 (41,4%) | **18/29 (62,1%)** |
| carro | 10/26 (38,5%) | **16/26 (61,5%)** |
| mercosul | 8/19 (42,1%) | **14/19 (73,7%)** |
| antigo | 4/10 (40,0%) | 4/10 (40,0%) |
| latência/recorte (contínuo) | 307,9 ms | **142,2 ms** |

O "antes" é a arbitragem entre engines: o `AutoOCR` escolhia um engine pelo layout do
recorte e usava o outro só como fallback. O "depois" é pool plano: todos os membros leem
e a fusão por caractere decide (`app/visao/consenso.py`). O ganho vem de diversidade de
MODELO: um modelo do fast-plate-ocr dá carro 13/26, três dão 17/26. Rodar o MESMO modelo
em 3 variantes de imagem mede 11/26, pior que ele sozinho.

Limitação conhecida: caractere `Q` em posição 4 de placas Mercosul é sistematicamente lido
como `O` pelos modelos EasyOCR e fast-plate-ocr (ambiguidade visual do Arial Bold em baixa
resolução).

## Placa de moto, o caso difícil

A placa de moto é o pior cenário do sistema, e por um motivo físico: ela é pequena
(~200×170 mm contra 400×130 do carro), fica baixa e inclinada, e a moto costuma parar
mais longe da câmera que o carro. Medido numa cena real de posto (frame 1280×720, moto
parada na bomba), a placa chega com **38×35 px**, contra ~56×30 do carro na mesma cena,
que sai com confiança 0.87.

**Detecção** (resolvido). O detector faz *letterbox* de tudo o que recebe para o lado do
input do modelo (608 px), então numa área de bico grande a placa de moto chegava pequena
demais e não era detectada. O que foi medido nessa cena:

| Enquadramento entregue ao detector | Resultado |
|---|---|
| Área do bico inteira (397×610) | nada, mesmo baixando o limiar a 0.05 |
| Mesma área ampliada 2× / 3× | nada, inútil por construção: o modelo reduz de volta a 608 |
| Recorte fechado só na placa (de 38 a 153 px) | nada, não é só escala: o modelo precisa do veículo em volta |
| Janelas de ~250 a 320 px pegando moto + placa | **conf de 0.19 a 0.81** |

Não é escala, é **enquadramento**, e o loop de leitura repetia no tempo, nunca no
espaço: com a área do bico fixa, as 12 tentativas eram 12 recortes idênticos, logo 12
falhas idênticas. Daí o fallback `tiles_fallback_get` (ver `BuscaEmTiles` em
`app/visao/detector.py`), que revarre o recorte em janelas sobrepostas quando a passada
normal não acha nada. Custo zero quando acha (o caso comum, carro).

O estágio de veículo não ajuda aqui: o YOLOX classifica a moto ocluída pelo piloto e
pelo frentista como `bicycle` (0.50) ou só vê `person`. Incluir `bicycle` em
`veiculo_classes` foi testado e **não resolve**, porque o recorte resultante (98×119) é fechado
demais, cai no terceiro caso da tabela.

**OCR, dois defeitos corrigidos.** A placa de moto tem **duas linhas** (letras em cima,
dígitos embaixo) e isso quebrava o OCR de duas formas independentes, nenhuma delas de
resolução:

1. **`_ler_paddleocr` ficava só com a MAIOR caixa.** Regra certa para carro, em que a placa é o
   maior texto do crop e 'BRASIL'/cidade/UF são menores, e destrutiva para moto, onde as
   duas linhas são caixas separadas de tamanho parecido: metade da placa era descartada,
   sempre. Medido nas 27 placas de moto de `testes/dataset.json`: **0/27**, e em todas o
   retorno era uma linha sozinha (`YZA3456` saía `3456`, `NOP5Q67` saía `NOP`). Não era
   resolução: são sintéticas e limpas. Agora as caixas de tamanho comparável são unidas
   em ordem de leitura.
2. **`_remover_ruidos_mercosul` apagava o 1º caractere de cada linha.** Ela pinta os cantos
   esquerdos (20%×28% em cima, 18%×30% embaixo) para cobrir o QR do CRLV-e e o marcador
   "BR". Numa placa **antiga** de moto real do posto, `_remover_header` devolveu
   `e_mercosul=True` por engano (faixa metálica), a limpeza cobriu o `Y` e o `5`, e o
   Paddle passou de `NOI`+`5947` para nada. Agora, se a limpeza rodou e o OCR voltou vazio,
   a leitura é repetida sem ela.

| PaddleOCR no dataset | antes | depois |
|---|---|---|
| moto (27) | 0/27 (0%) | 22/27 (81,5%, sendo 26/27 com o hint `mercosul_moto`) |
| carro (14) | 13/14 (92,9%) | 13/14 (92,9%, sem regressão, mesma falha) |
| total (41) | 13/41 (31,7%) | 35/41 (85,4%) |

> **Esta tabela não vale mais, e fica aqui como registro do erro.** As 27 motos eram
> **sintéticas**: o dataset tinha 42 fotos e o commit `d49a78f`
> ("Remove as placas sinteticas do dataset de testes") o cortou para 29, justamente porque
> foto sintética invertia o sinal da medição. Hoje há 2 motos reais rotuladas, e no recorte
> real da OSL2659 o Paddle devolve string vazia. O hint `mercosul_moto` citado aqui foi
> removido em 25/08/2026, porque ele reescrevia caractere que o modelo havia lido com 0,99 de
> confiança. Os números válidos hoje estão em "Precisão OCR" acima.

**O que ainda não fecha.** Na moto real de 38×35 px, a leitura foi de `''` (nada) para
`NOT5947`, 5 dos 7 caracteres. Os dígitos saem certos e confiantes (`5947` a 0,998); as
letras não. A ~10 px de altura de caractere não há informação para recuperar, e ampliar o
recorte não cria o que a câmera não capturou. A saída aqui é de campo: aproximar/zoomar a
câmera, ou uma câmera dedicada ao box da moto, de modo que a placa chegue com ≥ 80 a 100 px
de largura.
