# API de integração: guia para sistemas externos

Documento único para quem vai **acoplar outro sistema ao nosso**: o que enviar, o que
volta, e o que fazer com cada resposta. Escrito para ser lido por quem não tem acesso a
este código.

Existem **três formas de acoplamento**, e a maioria das integrações usa só a primeira:

| Forma | Quem inicia | Para quê | Endpoint |
|---|---|---|---|
| **Leitura sob demanda** (pull) | O sistema do posto | "Que placa está no bico agora?", o caso principal | `GET /api/leitura` |
| **Consulta de histórico** (pull) | O sistema externo | Relatórios, conciliação, busca por placa | `GET /api/deteccoes`, `/api/placa/{placa}`, `/api/chamadas` |
| **Notificação** (push) | Nós | Avisar toda detecção do monitoramento contínuo | Webhook HTTP / WebSocket |

> Se você está desenvolvendo o roteador/automação de um posto, **o que interessa é a
> seção [Leitura sob demanda](#1-leitura-sob-demanda-com-get-apileitura)**. O detalhamento
> ainda mais aprofundado dessa rota (recomendações de campo, casos de moto, bico de duas
> câmeras) está em [INTEGRACAO_ROTEADOR.md](INTEGRACAO_ROTEADOR.md).

---

## Endereço base e convenções

```
http://<host-do-servidor>:14000
```

A porta é configurável (`porta`, em `config.txt`); **14000** é o padrão.

- Todas as respostas são **JSON UTF-8**, exceto as rotas de imagem (`image/jpeg`).
- Datas são **ISO 8601 em UTC** com offset: `"2026-08-25T21:00:28.140188+00:00"`.
- Não há versionamento na URL. O contrato é **aditivo**: campos novos podem aparecer,
  campos publicados não mudam de nome nem de significado. **Ignore chaves que você não
  conhece**, porque é o que mantém sua integração funcionando entre versões.
- Documentação Swagger gerada automaticamente: `GET /docs`.

---

## Autenticação

Existem **três credenciais diferentes**, com alcances diferentes. Escolher a errada é o
erro de integração mais comum.

| Credencial | Como enviar | O que abre |
|---|---|---|
| **Nenhuma** | — | `GET /api/leitura` (dos postos **sem** chave própria), `GET /api/healthz`, `/static/**` |
| **Chave do posto** | `X-API-Key: <chave>` ou `?api_key=<chave>` | `GET /api/leitura` daquele CNPJ + o preview dos bicos **daquele** posto |
| **Chave global do servidor** | `X-API-Key: <chave>` ou `?api_key=<chave>` | **Tudo**, com poderes de administrador |

Detalhes que importam:

- **A chave do posto é opt-in.** Enquanto o posto não tiver chave gerada, `/api/leitura`
  responde normalmente sem credencial nenhuma. Assim que a chave é gerada no painel
  (`/empresas` → "API/LGPD"), toda chamada com aquele CNPJ passa a exigi-la.
- **Chave do posto errada ou ausente devolve `404`, não `401`.** É proposital: para quem
  não tem a chave, nem confirmamos que aquele cadastro existe.
- **A chave global é credencial de administrador.** Ela abre cadastro, configuração e o
  histórico de **todos** os postos. Não a entregue ao sistema de um cliente: para isso
  existe a chave por posto.
- Sem a chave global configurada no servidor, as rotas de consulta de histórico só
  respondem a um navegador logado. Peça a chave a quem administra o servidor antes de
  planejar uma integração de pull.
- Requisição não autenticada em `/api/**` devolve `401 {"detail": "Não autenticado."}`.

---

## 1. Leitura sob demanda com `GET /api/leitura`

O caso central: o sistema do posto avisa que um abastecimento terminou, e nós tiramos uma
foto **naquele instante** e devolvemos a placa. Não há fila, não há polling, não há
pipeline contínuo envolvido: a foto é tirada por causa da sua chamada.

### Quando chamar

**No fim do abastecimento**, não antes e não durante. Chamar cedo captura o veículo ainda
enchendo o tanque; chamar tarde captura a pista vazia.

### A requisição

```http
GET /api/leitura?entidade=OPCAO&cnpj=12345678000111&automacao=1&bico=1 HTTP/1.1
Host: servidor:14000
X-API-Key: <chave do posto, se houver>
```

| Parâmetro | Obrigatório | O que é | Tolerância |
|---|---|---|---|
| `entidade` | sim | Nome da rede/grupo dona do posto | Só é conferido e logado; divergência **não** bloqueia |
| `cnpj` | sim | CNPJ do posto | Com ou sem pontuação, porque normalizamos para só dígitos |
| `automacao` | sim | Qual sistema de automação está chamando (fixo por instalação, quase sempre `1`) | Ignora espaços e maiúsculas/minúsculas |
| `bico` | sim | Número físico do bico que abasteceu | Ignora espaços e maiúsculas/minúsculas |
| `rapido` | não | `1` pede o **modo de captura rápida**, com resposta em poucos segundos, lendo menos. Ver [seção 1.1](#11-modo-de-captura-rápida) | Ausente = leitura completa, como sempre |

Todos são **query string**; não há corpo na requisição.

### Tempo de resposta: leia antes de codar o cliente

A chamada **pode levar até ~30 segundos**. Isso não é lentidão: o servidor tira várias
fotos e só devolve quando a leitura converge ou o orçamento de tempo acaba
(`leitura_timeout_seg`, hoje 28s).

- Configure o **timeout HTTP do cliente para 35 a 40 segundos**. Um cliente com timeout de
  10s vai abandonar leituras boas no meio.
- **Não bloqueie a liberação da bomba** esperando esta resposta. Trate-a como assíncrona
  em relação ao fluxo do abastecimento.
- Uma chamada por abastecimento. Retry só faz sentido em erro de rede, nunca para
  "tentar melhorar" a placa (é o que a chamada já faz internamente).

### 1.1 Modo de captura rápida

Quando esperar ~30 segundos não serve, `&rapido=1` devolve em **poucos segundos**,
lendo menos.

```
GET /api/leitura?entidade=..&cnpj=..&automacao=..&bico=..&rapido=1
```

O servidor troca de perfil: usa os mesmos modelos do monitoramento contínuo (que já roda
em tempo real), tira **uma** foto em vez de até doze, e desiste cedo quando a câmera não
entrega quadro. **O envelope da resposta não muda**: mesmas chaves, mesmos significados.

| | Completo (padrão) | Com `rapido=1` |
|---|---|---|
| Fotos analisadas | até 12 | 1 ou 2 |
| Teto do laço | 28 s | 5 s |
| Espera pela câmera | até 20 s | 2 s |
| Reforço de OCR para placa borrada | sim | **não** |
| Varredura em janelas (moto de longe) | sim | **não** |
| Dados do veículo | conforme configuração | só o que já está em cache |

**O que você perde**, concretamente: **placa borrada** e **moto distante** tendem a voltar
como `placa: null` ou `confirmada: false`. Não é aleatório: são exatamente os dois casos
que os recursos desligados existiam para resolver.

**O que NÃO muda:**

- `confirmada` mantém o mesmo significado e **o mesmo limiar**. Continua sendo o portão
  para cobrança. O modo rápido não afrouxa nada, ele lê menos, e isso aparece como mais
  `confirmada: false`.
- As chaves da resposta são as mesmas nos dois modos.

**Como saber qual modo atendeu:** a resposta traz `"modo": "rapido"` ou
`"modo": "completo"`, sempre preenchido. Se você pediu `rapido=1` e recebeu
`"modo": "completo"`, o modo está desligado no servidor e o motivo aparece em `avisos`, e
a chamada roda completa e pode levar os 30 segundos de sempre.

**Timeout HTTP:** nas chamadas com `rapido=1` pode cair para ~10 s. Nas demais, mantenha
os 35 a 40 s.

**Uma diferença de comportamento, não só de tempo:** quando a câmera não entrega quadro
dentro da espera curta, o modo rápido **desiste dela** em vez de tentar reconectar. Se
isso acontecer com *todas* as câmeras do bico, a resposta é **`503`**, e não um `200` com
`placa: null`. Com duas câmeras, basta uma entregar quadro para a leitura seguir normal.
Foi o caso medido em 26/08/2026 com uma câmera fora do ar: o modo rápido devolveu a placa
confirmada em ~7 s, enquanto a leitura completa gastou 117 s tentando reconectar à câmera
morta e terminou em `503` sem placa nenhuma.

Ou seja: no modo rápido, `503` é um desfecho mais frequente e **muito mais barato**.
Trate-o como já trata hoje: transitório, pode repetir mais tarde.

> **Quando usar cada um.** O modo rápido serve a fluxos em que uma resposta tardia não
> tem valor, como mostrar a placa ao atendente enquanto o carro ainda está na pista, por
> exemplo. Para vincular placa a abastecimento e cobrar, o modo completo continua sendo a
> escolha: ele existe porque ler certo importa mais do que ler rápido.

### Resposta com placa lida (HTTP 200)

```json
{
  "entidade": "OPCAO", "cnpj": "12345678000111", "automacao": "1", "bico": "1",
  "camera_id": 3,
  "bico_id": 2,
  "placa": "PGK2D93",
  "padrao": "mercosul",
  "confianca": 0.91,
  "acordo": 0.85,
  "confirmada": true,
  "tipo_veiculo": "carro",
  "votos_leitura": 4,
  "votos_snapshot": 5,
  "total_snapshots": 6,
  "votos_ocr": 2,
  "total_engines": 2,
  "detalhes_ocr": [
    {"engine": "fast_plate_ocr", "placa": "PGK2D93", "padrao": "mercosul", "confianca": 0.91}
  ],
  "tentativas": 6,
  "parada_motivo": "acordo",
  "modo": "completo",
  "n_cameras_votando": 1,
  "fontes": [
    {"camera_id": 3, "papel": "traseira", "estado": "usada", "motivo": "",
     "tentativas": 6, "bboxes": 6, "candidatos": 5}
  ],
  "avisos": []
}
```

#### Os três campos que a sua integração realmente usa

| Campo | Tipo | O que fazer com ele |
|---|---|---|
| `placa` | string \| null | A placa lida. `null` é resposta válida (ver abaixo) |
| `confirmada` | bool | **`true` = pode vincular ao abastecimento. `false` = mande para conferência humana** |
| `veiculo.combustivel` | string \| null | Só existe se o enriquecimento estiver ligado, ver [seção 2](#2-bloco-veiculo-com-os-dados-do-veículo) |

**`confirmada` é o portão.** Ela já aplica o limiar calibrado no servidor
(`leitura_acordo_minimo`, hoje 0.80). Não recrie esse corte do seu lado a partir de
`acordo`, porque se o limiar for recalibrado aqui, sua integração acompanha sozinha.

`"confirmada": false` **vem com placa preenchida**, e essa placa costuma estar certa,
mas não o suficiente para virar cobrança sem um par de olhos. É o caso típico de moto e
de placa suja/distante.

#### Os demais campos, para diagnóstico

| Campo | O que é |
|---|---|
| `padrao` | `"mercosul"` (AAA0A00) ou `"antigo"` (AAA0000) |
| `confianca` | de 0 a 1, confiança do OCR na leitura eleita |
| `acordo` | de 0 a 1, o número bruto por trás de `confirmada` |
| `tipo_veiculo` | `"moto"`, `"carro"` ou `null`, ou seja, **nossa estimativa pela imagem**, não cadastro |
| `votos_leitura` | Quantas leituras de OCR apoiam a placa. É o que decide `confirmada` |
| `votos_snapshot` / `total_snapshots` | Quantas **fotos** bateram, de quantas tiradas |
| `votos_ocr` / `total_engines` / `detalhes_ocr` | Quantos modelos concordaram, e o que cada um leu |
| `tentativas` | Rodadas do laço de leitura |
| `parada_motivo` | `"acordo"` (convergiu), `"timeout"` ou `"max_tentativas"` |
| `camera_id` / `bico_id` | Ids internos: a câmera de onde saiu a placa e o bico atendido |
| `modo` | `"completo"` ou `"rapido"`, qual perfil atendeu esta chamada |
| `n_cameras_votando` / `fontes` | Diagnóstico de bico com duas câmeras, pode ignorar |
| `avisos` | Problemas que a leitura contornou (ex.: uma câmera fora do ar) |

> `votos_snapshot: 1` com `votos_leitura: 4` **é uma leitura sólida**, com quatro modelos
> concordando sobre o mesmo recorte. Os dois campos contam coisas diferentes e ambos
> continuam válidos.

> Uma leitura com `avisos` **e** `confirmada: true` é uma leitura boa. O aviso é para
> quem cuida da infraestrutura do posto, não motivo para recusar a placa.

### Resposta sem nenhuma placa encontrada (HTTP 200)

```json
{
  "placa": null,
  "mensagem": "Nenhuma placa detectada nos frames — verifique o enquadramento da área do bico e se o veículo aparece dentro dela",
  "camera_id": 3, "bico_id": 2,
  "bboxes_detectadas": 0,
  "snapshots_analisados": 12, "tentativas": 12,
  "parada_motivo": "timeout",
  "modo": "completo",
  "fontes": [], "avisos": []
}
```

**Também é `200`.** Sem carro na área, placa oculta ou tempo esgotado não são erros de
integração. Note que o conjunto de chaves é **diferente** do caso com placa, então teste
`placa === null` antes de acessar `confirmada`, `acordo` etc.

`bboxes_detectadas` separa dois problemas que se resolvem de formas opostas:

- **`0`**: não achamos placa em foto nenhuma. É **enquadramento**: veículo fora da área
  do bico, ou placa longe/oculta demais.
- **`> 0`**: achamos a placa, mas nenhum recorte virou texto. É **resolução/nitidez**:
  precisa aproximar ou dar zoom na câmera; mexer na área do bico não muda nada.

`mensagem` é texto livre para leitura humana, então **não faça o seu código depender do
conteúdo dela**.

### Respostas de erro

| HTTP | Quando | Corpo | O que fazer |
|---|---|---|---|
| `404` | Cadastro não encontrado ou desativado; ou chave do posto ausente/incorreta | `{"detail": "Cadastro não encontrado no nível 'bico' (cnpj=... automacao=1 bico=99)"}` | Erro de **configuração**, não transitório. Não repita: confira cadastro e chave |
| `429` | Excesso de chamadas (60/min por IP, 30/min por CNPJ) | `{"detail": "Muitas requisições, tente novamente em instantes."}` | Espalhe as chamadas; um posto real nunca chega perto disso |
| `503` | Câmera não respondeu (RTSP offline, rede) | `{"detail": "Não foi possível conectar à câmera via RTSP (...)"}` | Infraestrutura do posto. Pode repetir mais tarde |

O texto entre aspas simples no `404` (`'empresa'`, `'automacao'`, `'bico'`, `'camera'`)
diz **em qual nível a busca parou**, e a mensagem distingue "não existe" de "existe mas
foi desativado", e a correção é diferente em cada caso.

Num bico de **duas câmeras**, uma câmera fora do ar **não** cai em `404`/`503`: a leitura
segue com a que funciona, devolve `200` com a placa e registra o problema em `avisos`. O
erro só acontece quando **nenhuma** câmera do bico está utilizável.

**Toda chamada, inclusive as recusadas, fica registrada** e pode ser consultada em
`GET /api/chamadas` ou no painel *Integração*, com o valor exato que foi recebido.

---

## 2. Bloco `veiculo` com os dados do veículo

Quando o enriquecimento está ligado no servidor, a resposta de `/api/leitura` ganha um
bloco `veiculo` com o que se sabe sobre a placa lida, principalmente o **tipo de
combustível**:

```json
{
  "veiculo": {
    "consulta": "ok",
    "origem": "cache",
    "consultado_em": "2026-08-24T13:02:11.512331+00:00",
    "motivo": "",
    "combustivel": "Alcool / Gasolina",
    "combustivel_sigla": "G",
    "marca": "VW", "modelo": "CROSSFOX",
    "ano": 2007, "ano_modelo": 2007,
    "cor": "Prata",
    "especie": "Passageiro",
    "tipo_veiculo": "Automovel",
    "situacao": "Sem restrição",
    "municipio": "São Leopoldo", "uf": "RS"
  }
}
```

**O bloco é aditivo e opcional.** Se o recurso estiver desligado, a chave `veiculo` não
aparece e a resposta fica idêntica à de antes. As chaves são **sempre as mesmas 16**, em
todos os desfechos. O que muda é `consulta`:

| `consulta` | Significa | O que fazer |
|---|---|---|
| `"ok"` | Há dados (que ainda podem estar incompletos) | Use os campos |
| `"inexistente"` | A placa **não consta** na base consultada | Definitivo. Trate como veículo sem cadastro; não tente de novo |
| `"indisponivel"` | Não foi possível consultar agora | Siga sem os dados; a próxima leitura tenta de novo |

**Teste `consulta`, nunca os campos.** `"inexistente"` e `"indisponivel"` chegam os dois
com tudo em `null`, mas pedem reações opostas.

> Em servidor no modo `manual` (o padrão), **`"indisponivel"` é o caso normal**, não uma
> falha: nada é consultado automaticamente, e o bloco só vem preenchido para placas que
> alguém já mandou consultar pelo painel.

`origem` diz de onde veio o dado: `"cache"` (nosso banco, instantâneo), `"api"`
(consultado na hora) ou `"feira"` (ficha local de demonstração, só em servidor em modo
feira, ver docs/INTEGRACAO_ROTEADOR.md); `null` quando `consulta` é `"indisponivel"` fora
da demonstração. `motivo` é texto livre para diagnóstico humano.

### Cuidados

- **`null` significa "o registro não informou"**, nunca "é o valor padrão". **Nunca
  presuma gasolina** quando `combustivel` vier `null`.
- **`veiculo.situacao` não é checagem de roubo/restrição em tempo real.** Os dados ficam
  em cache por até 180 dias; uma restrição registrada depois não aparece aqui. Se o posto
  precisa dessa verificação, ela tem de vir de uma consulta própria.
- **Dois campos com o mesmo nome:** `tipo_veiculo` no nível de cima (`"moto"`/`"carro"`)
  é a **nossa estimativa pela imagem**; `veiculo.tipo_veiculo` (`"Automovel"`,
  `"Motocicleta"`) é a classificação do **registro** da placa. Vocabulários distintos,
  fontes distintas, podem discordar, e nenhum é automaticamente "o certo".

---

## 3. Consulta de histórico (pull)

Rotas para um sistema externo puxar o que já foi lido: relatórios, conciliação, busca.
**Todas exigem a chave global** (`X-API-Key`).

### `GET /api/deteccoes`, o histórico de leituras

Filtros (todos opcionais): `placa` (busca parcial), `desde`, `ate` (ISO 8601),
`empresa_id`, `bico_id`, `limit` (de 1 a 500, padrão 50), `offset`,
`origem` (`producao`, o padrão, que exclui testes manuais, `teste` ou `todas`),
`tipo_veiculo` (`moto`, `carro`, `desconhecido`, `todos`).

Devolve uma **lista** ordenada da mais recente para a mais antiga:

```json
[
  {
    "id": 973,
    "placa": "JKF6146",
    "padrao": "antigo",
    "confianca": 0.9527,
    "acordo": 1.0,
    "confirmada": 1,
    "criado_em": "2026-08-25T21:00:28.140188+00:00",
    "origem": "pipeline",
    "snapshot": "/static/snapshots/20260825T210027_JKF6146.jpg",
    "frame": "/static/snapshots/20260825T210027_JKF6146_frame.jpg",
    "bbox": "{\"x\": 588, \"y\": 551, \"w\": 105, \"h\": 34}",
    "camera_id": "rtsp", "camera_db_id": 3,
    "bico_id": null, "bico_codigo": null, "bico_nome": null,
    "empresa_id": 1, "empresa_nome": "POSTO EXEMPLO", "empresa_cnpj": "12345678000111",
    "tipo_veiculo": "carro", "veiculo_classe": 2, "veiculo_conf": 0.8888,
    "tipo_veiculo_fonte": "veiculo"
  }
]
```

Atenção a duas particularidades desta rota (é a projeção direta do banco):

- **`confirmada` vem como `0`/`1`**, não `true`/`false`.
- **`bbox` vem como *string* JSON**, não objeto, e precisa de um segundo parse.
- `origem` diz de onde veio a leitura: `"roteador"` (chamada de abastecimento),
  `"pipeline"` (monitoramento contínuo), `"teste"` (disparada do painel) ou `"feira"`
  (demonstração que fica FORA do filtro `producao` de propósito, para dado sintético não
  entrar na taxa de acerto).
- Leituras anteriores ao multi-tenant, e as do modo contínuo, vêm com `bico_id: null`.

### `GET /api/placa/{placa}`, o resumo consolidado de uma placa

```json
{
  "placa": "PGK2D93",
  "padrao": "mercosul",
  "lista": "branca",
  "lista_descricao": "Frota interna",
  "total_deteccoes": 137,
  "ultima_deteccao": {},
  "historico": []
}
```

`lista` é `"branca"`, `"negra"` ou `null`. `ultima_deteccao` é um objeto de detecção (com
`bbox` já convertido para objeto) e `historico` traz até 9 anteriores.
`total_deteccoes` é o total real, não o tamanho do `historico`.

### `GET /api/veiculos?placas=AAA1234,BBB5678`, dados de veículo em lote

Devolve `{"<placa>": <bloco veiculo> | null}`. **Nunca consulta a API externa**, devolve só o
que já está em cache. Até 500 placas por chamada. `null` = ainda não consultada.

### `GET /api/chamadas`, a auditoria das chamadas de leitura

Todas as chamadas recebidas em `/api/leitura`, **inclusive as recusadas**. Filtros:
`limit` (de 1 a 500), `empresa_id`, `status`, `apenas_erros=true`.

```json
[
  {
    "id": 52, "criado_em": "2026-08-25T18:30:43.411769+00:00",
    "entidade": "OPCAO", "cnpj": "12345678000111", "automacao": "1", "bico": "2",
    "bico_id": 3, "empresa_id": 1, "empresa_nome": "POSTO EXEMPLO",
    "status": "nao_confirmada",
    "motivo": "consenso insuficiente: acordo 1.00, 1/1 fotos (parada: timeout)",
    "placa": "SKU7G13", "acordo": 1.0, "tentativas": 1, "duracao_ms": 30583,
    "modo": "completo"
  }
]
```

`modo` diz qual perfil atendeu a chamada. Chamadas anteriores a este campo aparecem como
`"completo"`, que é o que elas de fato foram.

| `status` | Significa |
|---|---|
| `ok` | Placa lida e confirmada |
| `nao_confirmada` | Devolveu placa, mas sem consenso suficiente |
| `sem_placa` | Chamada atendida, nenhuma placa encontrada |
| `erro_cadastro` | `404`/`429`: cadastro, chave ou rate limit |
| `erro_camera` | `503`: câmera não respondeu |

**É aqui que se depura uma integração nova:** se a sua chamada não aparece nesta lista,
ela não chegou ao servidor.

### `GET /api/chamadas/resumo?horas=24`, os agregados

```json
{
  "horas": 24, "total": 412, "ok": 331, "sem_placa": 40, "nao_confirmada": 28,
  "erro_cadastro": 9, "erro_camera": 4,
  "taxa_sucesso": 0.803, "acordo_medio": 0.876, "duracao_media_ms": 9120,
  "por_posto": [{"posto": "POSTO EXEMPLO", "ok": 331, "total": 412}],
  "por_modo": [
    {"modo": "completo", "total": 380, "ok": 318, "duracao_media_ms": 9800, "taxa_sucesso": 0.837},
    {"modo": "rapido",   "total": 32,  "ok": 13,  "duracao_media_ms": 2100, "taxa_sucesso": 0.406}
  ],
  "motivos": [{"motivo": "cadastro: não encontrado no nível 'bico'", "n": 9}]
}
```

**Use `por_modo`, não só `taxa_sucesso`.** As duas populações têm taxa de acerto e duração
diferentes por desenho: o modo rápido lê menos em troca de responder antes. Somados num
número só, a adoção do modo rápido parece uma queda de qualidade, e a investigação vai
parar nas câmeras, onde não há nada de errado.

### `GET /api/listas`, as listas branca/negra

`GET /api/listas?tipo=branca` lista; `POST /api/listas` com
`{"placa": "AAA1234", "tipo": "branca", "descricao": "..."}` insere (`409` se já existe);
`DELETE /api/listas/{id}` remove. Inserir e remover exigem chave global.

### `GET /api/healthz`, o liveness

Público, sem dado nenhum: `{"status": "ok"}`. É o que apontar num monitor externo. O
`GET /api/health` detalhado (por câmera) exige credencial.

---

## 4. Notificação (push)

Para quem quer **receber** em vez de perguntar. Origem: o **monitoramento contínuo** das
câmeras, não as chamadas de `/api/leitura`.

### Webhook HTTP

Configure `webhook_url` no servidor e ligue `webhook_todas=sim`. A cada detecção emitida
pelo modo contínuo, fazemos um `POST` JSON:

```json
{
  "bomba": "1", "lado": "A",
  "placa": "PGK2D93", "padrao": "mercosul",
  "confianca": 0.913,
  "snapshot": "/static/snapshots/20260721T185912_PGK2D93.jpg"
}
```

Com `alerta_lista_negra=sim`, uma placa da lista negra dispara um POST adicional:

```json
{"placa": "PGK2D93", "padrao": "mercosul", "descricao": "Veículo bloqueado", "alerta": "lista_negra"}
```

Contrato de entrega, para você dimensionar o receptor:

- **Timeout de 5 segundos**, disparo em thread separada, então responda rápido e processe
  depois.
- **Não há retry.** Falhou, o evento se perde (fica registrado no nosso log).
- **Não há assinatura nem autenticação** no POST. Se o endereço não estiver em rede
  interna, proteja-o por outro meio (allowlist de IP, URL com segredo no path).
- Não confunda com `/api/leitura`: **a leitura reativa não dispara webhook.**

### WebSocket `/ws`

Feed em tempo real das detecções do modo contínuo, de **todas** as câmeras do processo.
Exige sessão de administrador ou a **chave global**:

```
ws://<host>:14000/ws?api_key=<chave global>
```

Mensagens recebidas:

```json
{"tipo": "deteccao", "placa": "PGK2D93", "padrao": "mercosul", "confianca": 0.913,
 "snapshot": "/static/snapshots/20260825T210027_PGK2D93.jpg",
 "criado_em": "2026-08-25T21:00:28.140188+00:00",
 "bomba": "1", "lado": "A", "lista": null, "tipo_veiculo": "carro"}
```

O servidor ignora o que você enviar; mande qualquer texto periodicamente para manter a
conexão viva. Conexão sem credencial válida é fechada com código **1008**.

Por não ser escopado por posto, o `/ws` é uma ferramenta de diagnóstico/console central, e
**não** o canal para entregar dados a um cliente específico.

---

## 5. Imagens

**As imagens não saem em `/api/leitura`.** A resposta da leitura não traz link de foto,
nem o recorte da placa, nem o quadro analisado. A foto é do posto e vive no sistema web
(histórico, tela do bico, editor de ROI), para quem entra com login.

As rotas de imagem abaixo continuam existindo para o **painel** e para integrações de
histórico que já usam a chave global. Nenhuma delas é pública:

| URL | Acesso | O que é |
|---|---|---|
| `/static/snapshots/<arquivo>.jpg` | Sessão do painel ou **api_key global** | Registro histórico: recorte da placa e quadro daquela detecção. É o que vem em `snapshot`/`frame` nas rotas de histórico |
| `/api/bicos/{bico_id}/preview.jpg` | Sessão do painel ou **chave do posto** | Último quadro analisado naquele bico |

O preview é privado porque `bico_id` é um inteiro sequencial compartilhado entre todos os
postos, e uma URL aberta permitiria varrer a foto mais recente de qualquer bomba de
qualquer cliente. A chave de um posto só abre os previews dos bicos **daquele** posto.

`/static/snapshots/` deixou de ser público em 27/08/2026: os arquivos são nomeados
`{timestamp}_{PLACA}.jpg`, então quem soubesse a placa e a janela de tempo varria a pasta
por força bruta, sem login. A api_key **do posto** não abre esta rota, porque o nome do arquivo
não carrega a identidade do posto, então não há como escopá-la por cliente.

O preview é **sobrescrito a cada leitura**: serve para mostrar "foi isto que a câmera viu"
logo após a chamada, não como arquivo histórico (esse é o `snapshot`).

Num bico de duas câmeras, `?camera_id=<id>` traz o quadro de uma câmera específica, e os
ids válidos vêm no array `fontes` da resposta.

## 6. Testando sem esperar um abastecimento

Três caminhos, do mais simples ao mais completo:

1. **Botão "Testar como o roteador"**, na tela do posto (`/posto/{id}`) do painel. Monta
   e dispara a chamada real com os dados já cadastrados e **mostra a URL usada**. É o
   jeito mais rápido de descobrir os valores exatos de `entidade`/`cnpj`/`automacao`/`bico`.
2. **`POST /api/bicos/{id}/ler-placa-teste`**, a mesma leitura, sem precisar montar a URL
   completa. Devolve o mesmo payload. As detecções ficam marcadas com `origem: "teste"` e
   não poluem a contagem de produção.
3. **A própria `GET /api/leitura`**, que pode ser chamada à vontade dentro do rate limit;
   ela sempre tira uma foto nova.

Depois de cada teste, confira `GET /api/chamadas?limit=5` para ver como o servidor
recebeu e classificou a sua chamada.

---

## Checklist de integração

Para o time que está acoplando:

- [ ] Confirmar **host e porta** do servidor, e se há **chave do posto** configurada.
- [ ] Timeout HTTP do cliente em **35 a 40 segundos** (~10 s nas chamadas com `rapido=1`).
- [ ] Decidir se algum fluxo usa **`rapido=1`** e, se usar, aceitar mais
      `placa: null` e `confirmada: false` nele.
- [ ] A chamada **não bloqueia** a liberação da bomba.
- [ ] Ler `placa`, e **testar `placa != null` antes de tudo** (o conjunto de chaves muda).
- [ ] Usar **`confirmada`** como portão para cobrança; `false` vai para conferência humana.
- [ ] Não derivar corte próprio de `acordo`, `confianca` ou `votos_*`.
- [ ] Não parsear `mensagem` nem `motivo`, que são texto humano.
- [ ] **Ignorar chaves desconhecidas** (o contrato é aditivo).
- [ ] Tratar `404` como erro de configuração (não repetir) e `503` como transitório.
- [ ] Se for exibir a foto ao atendente, ter a **chave do posto** para o `frame_url`.
- [ ] Se for usar o bloco `veiculo`, testar **`veiculo.consulta`**, nunca os campos, e
      nunca presumir combustível quando vier `null`.

---

## Referências

- [INTEGRACAO_ROTEADOR.md](INTEGRACAO_ROTEADOR.md): detalhamento de campo do
  `GET /api/leitura`: bico de duas câmeras, casos de moto, ressalvas dos dados do veículo.
- `/documentacao` (no próprio servidor): referência de **todos** os endpoints, incluindo
  cadastro e administração, com executor interativo.
- `/docs`: Swagger gerado automaticamente pelo FastAPI.
