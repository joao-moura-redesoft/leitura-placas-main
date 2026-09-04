# Leitura de placa sob demanda — `GET /api/leitura`

Documento para quem vai **consumir** o endpoint. Cobre só a captura de placa: o que
enviar e o que volta. Para as demais rotas (histórico, webhook, imagens), ver
`docs/API_INTEGRACAO.md`.

---

## O que este endpoint faz

O seu sistema avisa que um abastecimento terminou; nós tiramos uma foto **naquele
instante** e devolvemos a placa lida.

Não há fila, polling nem pipeline contínuo: a foto existe por causa da sua chamada.

**Quando chamar:** no **fim** do abastecimento. Cedo demais pega o veículo ainda enchendo
o tanque; tarde demais pega a pista vazia. **Uma chamada por abastecimento** — o retry só
faz sentido em erro de rede, nunca para "tentar melhorar" a placa (é o que a chamada já
faz internamente, tirando várias fotos).

---

## A requisição

```http
GET /api/leitura?entidade=OPCAO&cnpj=12345678000111&automacao=1&bico=1 HTTP/1.1
Host: <host-do-servidor>:14000
X-API-Key: <chave do posto, se houver>
```

Tudo vai na **query string**. Não há corpo na requisição.

| Parâmetro | Obrigatório | O que é | Tolerância |
|---|---|---|---|
| `entidade` | sim | Nome da rede/grupo dona do posto | Só é conferido e registrado; divergência **não** bloqueia a leitura |
| `cnpj` | sim | CNPJ do posto | Com ou sem pontuação — normalizamos para só dígitos |
| `automacao` | sim | Qual sistema de automação está chamando (fixo por instalação, quase sempre `1`) | Ignora espaços e maiúsculas/minúsculas |
| `bico` | sim | Número físico do bico que abasteceu | Ignora espaços e maiúsculas/minúsculas |
| `rapido` | não | `1` pede o modo de captura rápida (ver no fim) | Ausente = leitura completa |

### Autenticação

A chave por posto é **opt-in**:

- Enquanto o posto **não** tiver chave gerada, o endpoint responde **sem credencial
  nenhuma**.
- Depois que a chave é gerada, toda chamada daquele CNPJ passa a exigi-la, em
  `X-API-Key: <chave>` (header) ou `?api_key=<chave>` (query).
- **Chave errada ou ausente devolve `404`, não `401`** — proposital: a quem não tem a
  chave, não confirmamos sequer que o cadastro existe.

### Timeout do seu cliente — leia antes de codar

A chamada **pode levar até ~30 segundos**. Não é lentidão: o servidor tira várias fotos e
só devolve quando a leitura converge ou o tempo acaba.

- Configure o **timeout HTTP em 35–40 segundos**. Um cliente com 10s abandona leituras boas
  no meio.
- **Não bloqueie a liberação da bomba** esperando a resposta. Trate-a como assíncrona em
  relação ao fluxo do abastecimento.

---

## Resposta — placa lida (HTTP 200)

```json
{
  "entidade": "OPCAO", "cnpj": "12345678000111", "automacao": "1", "bico": "1",
  "placa": "PGK2D93",
  "confirmada": true,
  "padrao": "mercosul",
  "confianca": 0.91,
  "acordo": 0.85,
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
  "camera_id": 3,
  "bico_id": 2,
  "modo": "completo",
  "n_cameras_votando": 1,
  "fontes": [
    {"camera_id": 3, "papel": "traseira", "estado": "usada", "motivo": "",
     "tentativas": 6, "bboxes": 6, "candidatos": 5}
  ],
  "avisos": []
}
```

### Os dois campos que a sua integração realmente usa

| Campo | Tipo | O que fazer com ele |
|---|---|---|
| `placa` | string \| null | A placa lida. `null` é resposta válida — ver a seção seguinte |
| `confirmada` | bool | **`true` = pode vincular ao abastecimento. `false` = mande para conferência humana** |

**`confirmada` é o portão.** Ela já aplica o limiar calibrado no servidor. **Não recrie
esse corte do seu lado** a partir de `acordo`: se o limiar for recalibrado aqui, sua
integração acompanha sozinha.

`"confirmada": false` **vem com placa preenchida**, e essa placa costuma estar certa — só
não o bastante para virar cobrança sem um par de olhos. É o caso típico de moto e de placa
suja ou distante.

### Os demais campos, para diagnóstico

| Campo | O que é |
|---|---|
| `padrao` | `"mercosul"` (AAA0A00) ou `"antigo"` (AAA0000) |
| `confianca` | 0–1, confiança do OCR na leitura eleita |
| `acordo` | 0–1, o número bruto por trás de `confirmada` |
| `tipo_veiculo` | `"moto"`, `"carro"` ou `null` — **nossa estimativa pela imagem**, não cadastro |
| `votos_leitura` | Quantas leituras de OCR apoiam a placa. É o que decide `confirmada` |
| `votos_snapshot` / `total_snapshots` | Quantas **fotos** bateram, de quantas tiradas |
| `votos_ocr` / `total_engines` / `detalhes_ocr` | Quantos modelos concordaram, e o que cada um leu |
| `tentativas` | Rodadas do laço de leitura |
| `parada_motivo` | `"acordo"` (convergiu), `"timeout"` ou `"max_tentativas"` |
| `camera_id` / `bico_id` | Ids internos: a câmera de onde saiu a placa e o bico atendido |
| `modo` | `"completo"` ou `"rapido"` — qual perfil atendeu esta chamada |
| `n_cameras_votando` / `fontes` | Diagnóstico de bico com duas câmeras — pode ignorar |
| `avisos` | Problemas que a leitura contornou (ex.: uma das câmeras fora do ar) |

> `votos_snapshot: 1` com `votos_leitura: 4` **é uma leitura sólida** — quatro modelos
> concordando sobre o mesmo recorte. Os dois campos contam coisas diferentes.

> Leitura com `avisos` **e** `confirmada: true` é leitura boa. O aviso interessa a quem
> cuida da infraestrutura do posto; não é motivo para recusar a placa.

### Imagens não vêm no payload

A resposta **não traz link de foto** — nem o recorte da placa, nem o quadro analisado. A
imagem da leitura fica no sistema web do posto (histórico, tela do bico, editor de ROI),
para quem entra com login.

O que a integração recebe é a placa e os números que a sustentam. Para conferir visualmente
uma leitura duvidosa — o caso de `confirmada: false` —, o caminho é o painel do servidor.

---

## Resposta — nenhuma placa encontrada (HTTP 200)

```json
{
  "placa": null,
  "mensagem": "Nenhuma placa detectada nos frames — verifique o enquadramento da área do bico e se o veículo aparece dentro dela",
  "bboxes_detectadas": 0,
  "snapshots_analisados": 12,
  "tentativas": 12,
  "parada_motivo": "timeout",
  "camera_id": 3, "bico_id": 2,
  "modo": "completo",
  "fontes": [], "avisos": []
}
```

**Também é `200`.** Pista vazia, placa oculta ou tempo esgotado não são erros de
integração.

**O conjunto de chaves é diferente do caso com placa** — não há `confirmada`, `acordo`,
`confianca` nem `padrao`. **Teste `placa === null` antes de acessar qualquer outro campo.**

`bboxes_detectadas` separa dois problemas que se resolvem de formas opostas:

- **`0`** — não achamos placa em foto nenhuma. É **enquadramento**: veículo fora da área do
  bico, ou placa longe/oculta demais.
- **`> 0`** — achamos a placa, mas nenhum recorte virou texto. É **resolução/nitidez**:
  precisa aproximar ou dar zoom na câmera; mexer na área do bico não muda nada.

`mensagem` é texto livre para leitura humana — **não faça o seu código depender do
conteúdo dela**.

---

## Bloco `veiculo` (opcional)

Quando o enriquecimento de dados está ligado no servidor, a resposta ganha um bloco
`veiculo` com o que se sabe sobre a placa lida — principalmente o **tipo de combustível**:

```json
{
  "veiculo": {
    "consulta": "ok",
    "origem": "cache",
    "consultado_em": "2026-08-24T13:02:11.512331+00:00",
    "motivo": "",
    "combustivel": "Alcool / Gasolina",
    "combustivel_sigla": "F",
    "marca": "TOYOTA", "modelo": "ETIOS HB XPLUS MT",
    "ano": 2020, "ano_modelo": 2020,
    "cor": "PRATA",
    "especie": "Passageiro",
    "tipo_veiculo": "Automovel",
    "situacao": "Sem restrição",
    "municipio": "João Pessoa", "uf": "PB"
  }
}
```

**O bloco é aditivo e opcional.** Com o recurso desligado, a chave `veiculo` simplesmente
não aparece. Quando aparece, traz **sempre as mesmas 16 chaves** — o que muda é `consulta`:

| `consulta` | Significa | O que fazer |
|---|---|---|
| `"ok"` | Há dados (que ainda podem estar incompletos) | Use os campos |
| `"inexistente"` | A placa **não consta** na base consultada | Definitivo. Trate como veículo sem cadastro; não tente de novo |
| `"indisponivel"` | Não foi possível consultar agora | Siga sem os dados; a próxima leitura tenta de novo |

**Teste `consulta`, nunca os campos.** `"inexistente"` e `"indisponivel"` chegam os dois
com tudo em `null`, mas pedem reações opostas.

`origem` diz de onde veio o dado: `"cache"` (nosso banco, instantâneo), `"api"`
(consultado na hora) ou `"feira"` (ficha local de demonstração — só em servidor em modo
feira, ver docs/INTEGRACAO_ROTEADOR.md). `motivo` é texto livre para diagnóstico humano.

### Cuidados

- **`null` significa "o registro não informou"** — nunca "é o valor padrão". **Nunca
  presuma gasolina** quando `combustivel` vier `null`.
- **`veiculo.situacao` não é checagem de roubo/restrição em tempo real.** O dado fica em
  cache por até 180 dias; uma restrição registrada depois não aparece aqui.
- **Dois campos com o mesmo nome:** `tipo_veiculo` no nível de cima (`"moto"`/`"carro"`) é
  a **nossa estimativa pela imagem**; `veiculo.tipo_veiculo` (`"Automovel"`,
  `"Motocicleta"`) é a classificação do **registro** da placa. Vocabulários e fontes
  distintos — podem discordar.

---

## Respostas de erro

| HTTP | Quando | Corpo | O que fazer |
|---|---|---|---|
| `404` | Cadastro não encontrado ou desativado; ou chave do posto ausente/incorreta | `{"detail": "Cadastro não encontrado no nível 'bico' (cnpj=... automacao=1 bico=99)"}` | Erro de **configuração**, não transitório. Não repita: confira cadastro e chave |
| `429` | Excesso de chamadas (**60/min por IP**, **30/min por CNPJ**) | `{"detail": "Muitas requisições — tente novamente em instantes."}` | Espalhe as chamadas; um posto real não chega perto disso |
| `503` | Nenhuma câmera do bico respondeu (RTSP offline, rede) | `{"detail": "Não foi possível conectar à câmera via RTSP (...)"}` | Infraestrutura do posto. Transitório — pode repetir mais tarde |

O texto entre aspas simples no `404` (`'empresa'`, `'automacao'`, `'bico'`, `'camera'`) diz
**em qual nível a busca parou**, e a mensagem distingue "não existe" de "existe mas foi
desativado" — a correção é diferente em cada caso.

Num bico de **duas câmeras**, uma câmera fora do ar **não** vira erro: a leitura segue com
a que funciona, devolve `200` com a placa e registra o ocorrido em `avisos`. O erro só
acontece quando **nenhuma** câmera está utilizável.

---

## Modo de captura rápida (`&rapido=1`)

Quando esperar ~30 segundos não serve, `&rapido=1` responde em **poucos segundos** — lendo
menos.

| | Completo (padrão) | Com `rapido=1` |
|---|---|---|
| Fotos analisadas | até 12 | 1–2 |
| Teto do laço | 28 s | 5 s |
| Espera pela câmera | até 20 s | 2 s |
| Timeout HTTP sugerido | 35–40 s | ~10 s |

**O envelope da resposta não muda** — mesmas chaves, mesmos significados, mesmo limiar de
`confirmada`.

**O que você perde:** placa borrada e moto distante tendem a voltar como `placa: null` ou
`confirmada: false`. Não é aleatório — são exatamente os dois casos que os recursos
desligados no modo rápido existem para resolver.

**Como saber qual modo atendeu:** o campo `modo` da resposta, sempre preenchido. Se você
pediu `rapido=1` e recebeu `"modo": "completo"`, o modo está desligado no servidor, o
motivo aparece em `avisos`, e a chamada pode levar os 30 segundos de sempre.

> **Qual usar.** O modo rápido serve a fluxos em que resposta tardia não tem valor — mostrar
> a placa ao atendente enquanto o carro ainda está na pista. Para **vincular placa a
> abastecimento e cobrar**, use o modo completo: ele existe porque ler certo importa mais do
> que ler rápido.

---

## Checklist de integração

- [ ] Timeout HTTP em **35–40 s** (ou ~10 s se usar `rapido=1`)
- [ ] Chamada disparada **no fim** do abastecimento, uma por abastecimento
- [ ] **Não** bloquear a bomba esperando a resposta
- [ ] Testar `placa === null` **antes** de ler qualquer outro campo
- [ ] Usar `confirmada` como portão de cobrança; `false` → fila de conferência humana
- [ ] Testar `veiculo.consulta` antes dos campos do veículo; nunca presumir combustível
- [ ] **Ignorar chaves desconhecidas** — o contrato é aditivo: campos novos podem aparecer,
      campos publicados não mudam de nome nem de significado
- [ ] `404` → parar e corrigir cadastro/chave. `503`/rede → pode repetir mais tarde
