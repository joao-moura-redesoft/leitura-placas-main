# Casos de Uso do Sistema ALPR v2.1

## Visão Geral

Sistema de reconhecimento automático de placas veiculares (ALPR) desenvolvido para postos de combustível e estacionamentos. Integra detecção via YOLO26n ONNX, OCR multi-engine, câmeras IP Intelbras, rastreamento de veículos (Tracker) e monitoramento de saúde (WorkerSupervisor).

---

## UC-01: Leitura Automática de Placa no Abastecimento

**Ator:** Sistema (pipeline automático)  
**Gatilho:** Veículo para em frente à bomba de combustível.

**Fluxo principal:**
1. Câmera IP captura frames contínuos (padrão: 15 fps)
2. Se ROI configurado, o frame é recortado para a área de interesse antes da detecção
3. Detector YOLO identifica placa no frame
4. Tracker associa detecção ao veículo (track_id) e pula o OCR se já identificado recentemente
5. OCR extrai texto da região detectada
6. Validador normaliza e classifica (Mercosul / Antigo)
7. Tracker acumula votos; ao atingir `tracker_votos_emitir`, a placa é emitida
8. Detecção é registrada no banco com `bomba`, `lado`, `placa`, `confiança` e snapshot
9. Webhook opcional notifica sistema externo (ERP / PDV)

**Resultado:** Registro de detecção vinculado à bomba e lado corretos.

**Pré-condições:**
- Câmera cadastrada e associada a uma bomba/lado
- Placa dentro do campo de visão (ou dentro do ROI configurado)

**Configurações relevantes:** `frames_consenso`, `cooldown_seg`, `deteccao_fps_max`, `tracker_ativo`, `webhook_url`

---

## UC-02: Leitura Manual de Placa (Disparo Único)

**Ator:** Operador / Sistema externo via API  
**Gatilho:** Chamada `POST /api/cameras/{id}/ler-placa`

**Fluxo principal:**
1. Requisição indica câmera alvo
2. Sistema captura N snapshots (`snapshots_votacao`, padrão: 3)
3. Cada snapshot passa por YOLO + OCR independentemente
4. Sistema elege placa por votação de maioria entre frames
5. Votação secundária entre engines OCR mede concordância
6. Resposta retorna: `bomba`, `lado`, `placa`, `padrao`, `confianca`, `votos_snapshot`, `votos_ocr`, `frame_url`

**Resultado:** Leitura pontual com indicador de confiança para integração com outros sistemas.

**Exemplo de uso:** Operador de caixa pressiona botão no PDV → PDV chama API → recebe placa vinculada à bomba.

---

## UC-03: Consulta de Histórico de Placa

**Ator:** Operador / Sistema externo  
**Gatilho:** Chamada `GET /api/placa/{placa}` ou `GET /api/deteccoes`

**Fluxo principal:**
1. Requisição informa placa (ex: `ABC1D23`)
2. Sistema retorna:
   - Status na lista branca/negra
   - Total de detecções históricas
   - Última detecção (câmera, bomba, lado, snapshot, confiança)
   - Histórico das últimas 9 detecções

**Resultado:** Rastreabilidade completa de um veículo no estabelecimento.

**Filtros disponíveis em `/api/deteccoes`:** `placa`, `desde`, `ate`, `limit`, `offset`

---

## UC-04: Alerta de Veículo em Lista Negra

**Ator:** Sistema (automático ao detectar placa)  
**Gatilho:** Placa detectada está cadastrada como `negra` em `listas_placas`.

**Fluxo principal:**
1. Detecção automática ou manual identifica placa
2. Sistema consulta `listas_placas` e encontra tipo `negra`
3. Se `alerta_lista_negra=sim`, dispara `POST` para `webhook_url` com dados da detecção
4. Detecção é marcada com `lista=negra` e `lista_descricao` no retorno da API

**Resultado:** Equipe de segurança / sistema externo recebe alerta imediato.

**Configurações relevantes:** `alerta_lista_negra`, `webhook_url`

---

## UC-05: Gerenciamento de Listas Branca e Negra

**Ator:** Administrador  
**Interface:** `GET/POST/DELETE /api/listas` ou página `/listas`

**Fluxo principal:**
1. Administrador acessa página de listas ou chama API
2. Cadastra placa com tipo (`branca` = permitida, `negra` = bloqueada) e descrição
3. Sistema persiste no banco com chave UNIQUE por placa
4. A partir desse momento, toda detecção dessa placa inclui a classificação na resposta

**Resultado:** Controle de acesso ou rastreamento especial de veículos específicos.

---

## UC-06: Cadastro e Configuração de Câmera

**Ator:** Técnico / Administrador  
**Interface:** `POST/PUT /api/cameras` ou página `/cameras`

**Fluxo principal:**
1. Técnico informa: nome, bomba, lado, tipo de câmera (`usb`, `csi`, `rtsp`, `intelbras`)
2. Para Intelbras: host, porta, usuário, senha, canal, subtype, formato, transporte RTSP
3. Sistema valida unicidade da combinação `(bomba, lado)`
4. Pipeline dedicado é iniciado em thread separada para a câmera
5. Stream MJPEG fica disponível em `/stream/{id}.mjpg`
6. Técnico pode acessar `/roi/{id}` para configurar a área de captura

**Resultado:** Câmera operacional com detecção automática ativa.

**Erro esperado:** Conflito `(bomba, lado)` retorna HTTP 409.

---

## UC-07: Descoberta e Scan de Câmeras na Rede

**Ator:** Técnico  
**Interface:** `GET /api/cameras/rede-local` + `POST /api/cameras/scan` ou página `/cameras` (aba Scan)

**Fluxo principal:**
1. Técnico solicita descoberta da sub-rede local
2. Sistema retorna range de IPs da interface ativa
3. Técnico dispara scan na sub-rede
4. Sistema testa porta RTSP (554) em cada IP em paralelo
5. Retorna lista de IPs com RTSP acessível

**Resultado:** Lista de candidatos a câmera para configuração.

---

## UC-08: Monitoramento ao Vivo (Dashboard)

**Ator:** Operador  
**Interface:** Página `/` ou `/dashboard`

**Informações disponíveis:**
- Stream MJPEG ou HLS com bounding boxes em tempo real
- Feed das últimas 20 detecções com placa, câmera, timestamp
- **Painel de saúde das câmeras** com status por câmera (ok / sem_frame / parado), restarts e backoff atual
- Crop do último OCR (`/api/debug/ocr_crop`)
- Estatísticas: total de detecções, top 10 placas, FPS atual, uptime

**Resultado:** Visibilidade operacional completa sem necessidade de console. Operador identifica imediatamente câmeras com problema.

---

## UC-09: Configuração do Sistema

**Ator:** Administrador  
**Interface:** `GET/POST /api/config` ou página `/configuracao`

**Parâmetros principais configuráveis:**
- Engine OCR (`auto`, `easyocr`, `fast_plate_ocr`, `tesseract`, etc.)
- Sensibilidade de detecção (`conf_threshold`, `nms_threshold`)
- Comportamento do pipeline (`frames_consenso`, `cooldown_seg`, `deteccao_automatica`)
- Tracker (`tracker_ativo`, `tracker_ocr_intervalo`, `tracker_votos_emitir`)
- Streaming (`streaming_modo`: `mjpeg` ou `hls`)
- Webhook e alertas
- Nível de log

**Resultado:** Sistema reinicia pipeline com nova configuração sem reiniciar o processo.

---

## UC-10: Integração com Sistema Externo (ERP / PDV)

**Ator:** Sistema externo via API REST  
**Base URL:** `http://<host>:14000`

**Integrações típicas:**

| Operação | Endpoint | Uso |
|----------|----------|-----|
| Ler placa na bomba | `POST /api/cameras/{id}/ler-placa` | PDV solicita leitura no momento do abastecimento |
| Consultar veículo | `GET /api/placa/{placa}` | Verificar histórico ou lista negra antes de liberar acesso |
| Receber alertas | `webhook_url` configurado | Receber notificações push de detecções |
| Histórico filtrado | `GET /api/deteccoes?desde=&ate=` | Exportar movimentações por período |
| Status do sistema | `GET /api/status` | Health check da integração |
| Saúde das câmeras | `GET /api/health` | Verificar se câmeras estão operacionais |

**Autenticação:** Token de sessão via cookie (logar em `/login` antes de usar a API em contexto de browser). Para integração machine-to-machine, implementar token de API estático (pendente).

---

## UC-11: Detecção Multi-Câmera Simultânea

**Ator:** Sistema  
**Cenário típico:** Posto com 3 bombas × 2 lados = 6 câmeras simultâneas

**Comportamento:**
- Cada câmera roda em thread dedicada com pipeline e tracker independentes
- Frames de cada câmera disponíveis em `estado.frames_cameras[id]`
- Stream independente por câmera: `/stream/{id}.mjpg` ou `/hls/{id}/index.m3u8`
- Detecções registradas com respectivos `bomba` e `lado`
- `WorkerSupervisor` monitora todas as câmeras e reinicia falhas automaticamente

**Limitação Intelbras:** Apenas 1 conexão RTSP simultânea por câmera física, então o sistema reutiliza o frame do pipeline ativo.

---

## UC-12: Validação e Testes de Acurácia

**Ator:** Técnico / Desenvolvedor  
**Interface:** Página `/testes` + scripts em `testes/`

**Fluxo principal:**
1. Dataset de 42 placas (sintéticas + fotos reais) em `testes/`
2. Interface permite executar engines individuais ou comparar todos
3. Resultados exportados como JSON em `testes/resultados/`
4. Métricas: acurácia por engine, por padrão (Mercosul/Antigo), por condição de luz

**Acurácia referência (dataset interno):**

| Engine | Acurácia |
|--------|----------|
| AutoOCR (`auto`) | 92,9% |
| fast-plate-ocr | ~90% |
| EasyOCR | ~85% |
| Tesseract | ~70% a 75% |

---

## UC-13: Configuração de Área de Captura (ROI)

**Ator:** Técnico  
**Interface:** Página `/roi/{camera_id}`

**Fluxo principal:**
1. Técnico acessa a página de ROI da câmera
2. Clica em "Atualizar Imagem" para capturar o frame atual
3. Clica e arrasta para desenhar o retângulo de interesse sobre a imagem
4. Clica em "Ler Placa" para testar a leitura dentro da área
5. Se satisfatório, clica em "Salvar Área", que é aplicado imediatamente sem reiniciar o pipeline
6. Para remover a restrição, clica em "Usar Frame Completo"

**Resultado:** Pipeline passa a detectar apenas placas dentro da área configurada, o que elimina falsos positivos de fundos e cartazes fora da pista.

**Impacto técnico:** ROI crop é aplicado antes do YOLO; bboxes são deslocadas de volta às coordenadas originais para anotação correta no stream.

---

## UC-14: Autenticação e Acesso ao Sistema

**Ator:** Administrador / Operador  
**Interface:** `/criar-admin`, `/login`, `/logout`

**Fluxo de primeiro acesso:**
1. Primeira execução redireciona para `/setup` (wizard)
2. Após configuração inicial, sistema disponibiliza `/criar-admin`
3. Administrador cria conta (nome, e-mail, senha), aceito apenas se nenhum usuário existe
4. A partir daí, `/criar-admin` retorna 403 (bloqueado)

**Fluxo de login:**
1. Usuário acessa qualquer página → redirecionado para `/login` se não autenticado
2. Preenche e-mail e senha
3. Sistema verifica bcrypt hash → cria sessão (token UUID, 7 dias)
4. Cookie `sessao` HttpOnly é definido no browser
5. Qualquer chamada fetch à API que retorne 401 redireciona automaticamente para `/login`

**Fluxo de logout:**
1. Usuário clica em "Sair"
2. Sessão é removida do banco
3. Cookie é apagado
4. Redirecionamento para `/login`

**Resultado:** Acesso protegido por autenticação; sessão persiste 7 dias; múltiplos browsers/dispositivos suportados.

---

## UC-15: Monitoramento de Saúde das Câmeras

**Ator:** Operador / Sistema  
**Interface:** Dashboard (`/dashboard`) + `GET /api/health`

**Fluxo principal:**
1. `WorkerSupervisor` verifica a cada 5s: thread viva + frescor do último frame
2. Se thread morta ou frame > 30s sem atualizar → tenta reiniciar com backoff exponencial
3. Dashboard exibe card "Saúde das Câmeras" com status por câmera
4. `GET /api/health` expõe dados estruturados para integração com sistemas de monitoramento externos

**Estados reportados:**

| Status | Significado |
|--------|-------------|
| `ok` | Thread viva e frames chegando normalmente |
| `sem_frame` | Thread viva mas sem frame novo há > 15s |
| `parado` | Thread morta ou reiniciando (em backoff) |

**Resultado:** Operador identifica câmeras com problema sem precisar verificar logs; supervisor corrige automaticamente falhas transientes.

---

## Resumo dos Atores

| Ator | Descrição |
|------|-----------|
| **Sistema** | Pipeline automático de captura, rastreamento e detecção |
| **Operador** | Monitora dashboard, acompanha detecções ao vivo, usa painel de saúde |
| **Técnico** | Instala, configura câmeras, define ROI, valida acurácia |
| **Administrador** | Gerencia listas, configura parâmetros, cria contas de usuário |
| **Sistema Externo** | ERP, PDV ou qualquer cliente REST que consome a API |
