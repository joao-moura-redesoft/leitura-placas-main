# Integração com o roteador do posto (leitura reativa de placa)

Especificação para quem vai desenvolver a chamada HTTP do lado do posto (sidecar
Java/roteador) contra o servidor central de leitura de placas.

> Para a visão geral da API inteira — autenticação, consulta de histórico, webhook,
> WebSocket e imagens —, veja [API_INTEGRACAO.md](API_INTEGRACAO.md). Este documento aqui
> é o aprofundamento de campo de `GET /api/leitura`.

## Quando chamar

Faça a chamada **quando o abastecimento terminar** — não antes, não durante. A foto é
tirada na hora da chamada; chamar cedo demais captura o carro ainda enchendo o tanque
ou já saindo.

## A chamada

```
GET /api/leitura?entidade=<...>&cnpj=<...>&automacao=<...>&bico=<...>
```

| Parâmetro  | O que é                                              | Exemplo         |
|------------|-------------------------------------------------------|-----------------|
| `entidade` | Nome da rede/grupo dono do posto                       | `OPCAO`         |
| `cnpj`     | CNPJ do posto. Pode vir com ou sem pontuação — o servidor normaliza (mantém só dígitos) | `12.345.678/0001-11` ou `12345678000111` |
| `automacao`| Identifica QUAL sistema de automação está chamando (ver nota abaixo) | `1` |
| `bico`     | Identifica o bico físico que abasteceu                 | `1` |
| `rapido`   | **Opcional.** `1` pede o modo de captura rápida — resposta em poucos segundos, lendo menos (ver *Modo de captura rápida*, abaixo) | `1` |

**Exemplo real, com o cadastro atual do posto ALTIPLANO:**

```
GET /api/leitura?entidade=OPCAO&cnpj=12345678000111&automacao=1&bico=1
```

### O que `automacao` e `bico` precisam ser

Não são valores inventados pelo servidor — são o que faz sentido para o lado que está
desenvolvendo agora (vocês). Recomendação:

- **`automacao`**: um valor **fixo por instalação**, configurado uma vez no arquivo de
  configuração local do roteador quando ele é instalado naquele posto. Na prática vai
  ser sempre `"1"`, a menos que um posto real tenha dois sistemas de automação rodando
  ao mesmo tempo (raro, mas acontece — é o único motivo desse campo existir).
- **`bico`**: o **número físico do bico** que já vem do protocolo da bomba/automação —
  o mesmo número impresso na bomba. Evita inventar uma segunda numeração paralela que
  alguém precisaria manter sincronizada com a numeração real.

### Tolerância a diferenças de formatação

`automacao` e `bico` são comparados **ignorando espaço em branco e maiúscula/minúscula**.
`"1"`, `" 1 "` e `"1 "` são tratados como o mesmo valor. Isso existe porque ambos os
lados desta integração são código novo — não precisa se preocupar em bater exatamente
byte a byte.

O `cnpj` é normalizado do mesmo jeito: qualquer caractere que não seja dígito é
descartado antes de comparar.

## O que esperar de volta

### Sucesso, placa lida

```json
{
  "entidade": "OPCAO", "cnpj": "12345678000111", "automacao": "1", "bico": "1",
  "camera_id": 3, "bico_id": 2,
  "placa": "PGK2D93",
  "padrao": "mercosul",
  "confianca": 0.91,
  "acordo": 0.85,
  "confirmada": true,
  "votos_snapshot": 5, "total_snapshots": 6,
  "votos_leitura": 4,
  "votos_ocr": 2, "total_engines": 2,
  "detalhes_ocr": [{"engine": "fast_plate_ocr", "placa": "PGK2D93", "padrao": "mercosul", "confianca": 0.91}],
  "tentativas": 6,
  "parada_motivo": "acordo",
  "modo": "completo",
  "mockada": false
}
```
HTTP `200`. Use o campo `"placa"` — **e confira `"confirmada"` antes de vincular a placa
a um abastecimento.**

`"confirmada": false` significa que a leitura terminou sem atingir o consenso mínimo
configurado no servidor (o loop esgotou o tempo e devolveu a melhor candidata). Ela vem
com `"placa"` preenchida, mas **não deve ser tratada como placa boa**: encaminhe para
conferência do atendente em vez de cobrar direto. É o caso típico de moto e de placa
distante/suja. `"acordo"` (0 a 1) é o número bruto por trás dessa decisão, útil para
diagnóstico — mas prefira `"confirmada"`, que já aplica o limiar configurado no posto, em
vez de fixar um corte próprio no lado do roteador.

`"votos_leitura"` (novo em 25/08/2026) é quantas LEITURAS de OCR apoiam a placa devolvida,
e é ele que decide `"confirmada"` hoje. Não confundir com `"votos_snapshot"`, que continua
sendo quantas FOTOS bateram e não mudou de significado: com o ensemble de modelos, uma foto
rende 3-4 leituras independentes, então `votos_snapshot: 1` com `votos_leitura: 4` é uma
leitura sólida — quatro modelos concordando sobre o mesmo recorte. Nada precisa mudar no
lado do roteador: continue usando `"confirmada"`.

`"mockada"` (novo em 04/09/2026) sai nos dois desfechos (com e sem placa) e **em produção
é sempre `false`**. `true` significa que a placa devolvida NÃO veio do OCR: é um veículo de
demonstração cadastrado, num servidor em modo feira (ver "Dados do veículo" abaixo). O
campo existe para que uma resposta de demonstração nunca seja indistinguível de uma real —
antes dele o único sinal era uma frase em `avisos`, texto livre que nenhum consumidor
tipado lê.

Os demais campos (`votos_ocr`, `detalhes_ocr`, etc.) são detalhe interno de diagnóstico;
não é necessário processá-los.

### Dados do veículo (combustível)

Quando o servidor está configurado para consultar dados do veículo, a resposta ganha um
bloco `veiculo` com o que se sabe da placa lida — principalmente o **tipo de combustível**:

```json
{
  "veiculo": {
    "consulta": "ok",
    "origem": "cache",
    "consultado_em": "2026-08-24T13:02:11.512331+00:00",
    "motivo": "",
    "combustivel": "Alcool / Gasolina",
    "combustivel_sigla": "G",
    "marca": "VW",
    "modelo": "CROSSFOX",
    "ano": 2007,
    "ano_modelo": 2007,
    "cor": "Prata",
    "especie": "Passageiro",
    "tipo_veiculo": "Automovel",
    "situacao": "Sem restrição",
    "municipio": "São Leopoldo",
    "uf": "RS"
  }
}
```

**O bloco é aditivo e opcional.** Uma integração que ignora chaves desconhecidas continua
funcionando sem alteração nenhuma. Se o recurso estiver desligado no servidor, a chave
`veiculo` simplesmente não aparece — a resposta fica idêntica à de antes.

**As chaves são sempre as mesmas 16**, em todos os desfechos. O que muda é `consulta`:

| `consulta` | O que significa | O que fazer |
|---|---|---|
| `"ok"` | Há dados do veículo (que ainda podem estar incompletos — ver abaixo) | Use os campos |
| `"inexistente"` | A placa **não consta** na base consultada | Não adianta tentar de novo: o registro não existe lá. Trate como veículo sem cadastro |
| `"indisponivel"` | Não foi possível consultar agora | Siga sem os dados. A próxima leitura da mesma placa tenta de novo |

> **Em servidor no modo `manual`, `"indisponivel"` é o caso NORMAL, não uma falha.**
> Quando a cota de consultas é curta, o servidor pode ser configurado para não consultar
> nada sozinho: o bloco só vem preenchido para placas que alguém já mandou consultar pelo
> painel. O roteador não muda em nada por causa disso — mas quem for depurar precisa saber
> que `veiculo.consulta = "indisponivel"` na maioria das leituras pode ser a configuração
> escolhida, e não um problema para investigar.

**Teste `consulta`, não os campos.** `"inexistente"` e `"indisponivel"` chegam os dois com
os campos em `null`, mas pedem reações opostas: o primeiro é uma resposta definitiva sobre
aquele veículo, o segundo é um problema passageiro nosso ou do provedor.

`motivo` é texto livre para diagnóstico humano (`""` quando `consulta` é `"ok"`) — não faça
o roteador depender do seu conteúdo, pela mesma razão já dita sobre `mensagem`.

`origem` diz de onde veio o dado: `"cache"` (do nosso banco, resposta instantânea),
`"api"` (consultado na hora) ou `"feira"` (dado de DEMONSTRAÇÃO — ver abaixo). Serve para o
posto confirmar que o cache está funcionando sem precisar de acesso ao nosso banco. Vem
`null` quando `consulta` é `"indisponivel"` sem ter sido uma leitura de demonstração.

#### `mockada: true` — dado de demonstração

Servidor em **modo feira** (usado em estande de evento, e desligado por padrão) pode
devolver a leitura de um veículo de demonstração cadastrado. Nesse caso o payload traz
`"mockada": true` **no nível de cima** e o bloco `veiculo` vem preenchido a partir de uma
ficha local — sem nenhuma consulta externa, porque o evento acontece offline. É por isso
que o bloco aparece mesmo com a consulta de veículo desligada no servidor: sem rede e sem
token, uma consulta real só saberia devolver `"indisponivel"`.

O bloco tem exatamente a mesma forma e os mesmos tipos de uma consulta real: a integração
não precisa (e não deve) tratar este caso de modo especial. Os campos que permitem
distinguir, para quem precisar:

- `mockada` no nível de cima é `true`;
- `veiculo.origem` é `"feira"` (em vez de `"cache"`/`"api"`), e `veiculo.motivo` vem
  preenchido mesmo com `consulta: "ok"` — numa consulta real, `motivo` é `""` quando o
  desfecho é `"ok"`.

**Isto nunca acontece em posto de produção.** O mock é restrito a um posto de
demonstração específico, configurado à parte; sem ele o modo fica inerte.

#### Campo a campo

| Campo | Exemplo | Observação |
|---|---|---|
| `combustivel` | `"Alcool / Gasolina"` | O que o registro do veículo informa. É o campo que motiva este bloco |
| `combustivel_sigla` | `"G"` | Sigla vinda da tabela FIPE. Falta com frequência — **não** é derivada do texto acima |
| `marca`, `modelo` | `"VW"`, `"CROSSFOX"` | |
| `ano`, `ano_modelo` | `2007` | Número, não texto |
| `cor` | `"Prata"` | |
| `especie` | `"Passageiro"` | |
| `tipo_veiculo` | `"Automovel"` | **Não confundir** com o `tipo_veiculo` do nível de cima — ver abaixo |
| `situacao` | `"Sem restrição"` | **Leia a ressalva de validade abaixo antes de usar** |
| `municipio`, `uf` | `"São Leopoldo"`, `"RS"` | Município de emplacamento |

> **`null` significa "o registro consultado não informou"** — nunca "é o valor padrão". A
> base de terceiro é reconhecidamente incompleta, e o próprio fornecedor avisa que parte
> dos dados pode faltar em qualquer consulta. **Nunca presuma gasolina** (ou qualquer
> outro combustível) quando `combustivel` vier `null`: um `null` aqui e um `"Alcool /
> Gasolina"` são informações diferentes, e tratá-los igual é o erro que este bloco existe
> para evitar.

#### Dois campos com o mesmo nome, significados diferentes

| Onde | Valores | O que é |
|---|---|---|
| `tipo_veiculo` (nível de cima) | `"moto"`, `"carro"`, `null` | **Nossa** estimativa, feita pelo detector a partir da imagem |
| `veiculo.tipo_veiculo` | `"Automovel"`, `"Motocicleta"`, ... | Classificação do **registro** do veículo |

São vocabulários distintos, de fontes distintas. Podem discordar — e quando discordam,
nenhum dos dois é automaticamente "o certo": o de cima descreve o que a câmera viu agora,
o de baixo descreve o que está cadastrado para aquela placa.

#### Ressalva de validade

Os dados ficam guardados por até **180 dias** antes de serem consultados de novo, porque o
que interessa aqui não muda (combustível, marca, modelo). Mas isso vale para o bloco
inteiro, e **`situacao` muda**: uma restrição, um roubo ou um furto registrados depois da
nossa consulta não aparecem aqui.

**`veiculo.situacao` não é checagem de restrição ou roubo em tempo real.** Se o posto
precisa dessa verificação, ela tem de vir de uma consulta própria, feita na hora, e não
deste campo.

### Bico com duas câmeras

Um bico pode ser configurado no servidor com **duas câmeras** (uma vendo a traseira,
outra a frente do veículo), para os casos em que a placa de um lado fica encoberta —
estepe na traseira, carro colado, ângulo ruim. **Isso é configuração do servidor: a
chamada do roteador não muda em nada** — mesma URL, mesmos parâmetros, mesmo tempo de
resposta (as duas câmeras dividem o mesmo orçamento de tempo, não somam).

Os campos que você já usa mantêm exatamente o significado:

| Campo | Com duas câmeras |
|---|---|
| `placa`, `confirmada`, `acordo` | idênticos — o consenso considera as fotos das duas |
| `camera_id` | a câmera que **leu** a placa (antes: a única do bico) |

E aparecem dois campos novos, **puramente informativos** (pode ignorá-los):

```json
{
  "n_cameras_votando": 2,
  "fontes": [
    {"camera_id": 3, "papel": "traseira", "estado": "abandonada",
     "motivo": "sem detecção em 2 rodadas (2 foto(s))", "tentativas": 2, "bboxes": 0,
     "candidatos": 0},
    {"camera_id": 4, "papel": "frente", "estado": "usada", "motivo": "",
     "tentativas": 9, "bboxes": 7, "candidatos": 5}
  ],
  "avisos": []
}
```

`avisos` lista problemas que a leitura contornou (ex.: uma das câmeras fora do ar). Uma
leitura com aviso **e** com `confirmada: true` é uma leitura boa — o aviso é para quem
cuida da infraestrutura do posto, não motivo para recusar a placa.

### Sucesso na chamada, mas nenhuma placa encontrada

```json
{
  "placa": null,
  "mensagem": "Nenhuma placa detectada nos frames — verifique o enquadramento da área do bico e se o veículo aparece dentro dela",
  "camera_id": 3, "bico_id": 2, "bboxes_detectadas": 0,
  "snapshots_analisados": 12, "tentativas": 12, "parada_motivo": "timeout"
}
```
Também HTTP `200`. `"placa": null` é uma resposta válida — sem carro na área, placa
suja, ou tempo esgotado sem conseguir ler. Não é erro de integração.

`bboxes_detectadas` separa dois problemas que antes chegavam com a mesma mensagem, e que
se resolvem de formas opostas:

- **`0`** — o detector não achou placa em nenhuma foto. É enquadramento: veículo fora da
  área do bico, ou placa longe/oculta demais.
- **`> 0`** — achou a placa, mas nenhum recorte virou texto válido (a mensagem muda para
  "Placa localizada em N recorte(s)..."). É resolução/nitidez: a placa está no lugar
  certo, pequena ou borrada demais para o OCR. Mexer na área do bico não muda nada aqui —
  precisa aproximar/zoomar a câmera.

`mensagem` é texto livre para diagnóstico humano; não faça o roteador depender do seu
conteúdo (use `placa`, `confirmada` e, se quiser detalhar, `bboxes_detectadas`).

### Erro de cadastro (cnpj/automação/bico não encontrado, ou desativado)

```json
{"detail": "Cadastro não encontrado no nível 'bico' (cnpj=12345678000111 automacao=1 bico=99)"}
```
```json
{"detail": "Cadastro nível 'empresa' está desativado no cadastro (cnpj=12345678000111 automacao=1 bico=1)"}
```
HTTP `404` nos dois casos. O texto entre aspas simples (`'empresa'`, `'automacao'`,
`'bico'` ou `'camera'`) diz exatamente em qual nível a busca parou. A mensagem distingue
"não existe" de "existe mas foi desativado" — um posto/automação/bico/câmera
desativado no cadastro também bloqueia a leitura, não só o bico individualmente.
Toda chamada que cai aqui fica registrada no painel **Integração** do servidor,
mostrando o valor exato recebido.

Num bico de **duas câmeras**, uma câmera desativada ou fora do ar **não** cai aqui: a
leitura segue com a que funciona e devolve `200` com a placa, registrando o problema em
`avisos`. O `404` só acontece quando **nenhuma** das câmeras do bico está utilizável.

### Câmera não respondeu

```json
{"detail": "Não foi possível conectar à câmera via RTSP (...). Verifique o IP/host, porta, usuário e senha."}
```
HTTP `503`. Problema de infraestrutura do posto (câmera offline, rede), não de
integração.

## Imagens não vêm no payload

A resposta de `/api/leitura` **não traz link de imagem** — nem o recorte da placa, nem o
quadro analisado. A foto da leitura é do posto, e fica no sistema web: histórico, tela do
bico e editor de ROI, para quem entra com login.

O que a integração recebe é a placa e os números que a sustentam (`confirmada`, `acordo`,
`confianca`, `votos_*`). Para conferência visual de uma leitura duvidosa, o caminho é o
painel do servidor.

## Modo de captura rápida (`rapido=1`)

A chamada normal gasta até ~30 s procurando consenso. Quando o fluxo do posto não
comporta essa espera, acrescente `&rapido=1`:

```
GET /api/leitura?entidade=OPCAO&cnpj=12345678000111&automacao=1&bico=1&rapido=1
```

O servidor passa a usar os mesmos modelos do monitoramento contínuo — que já lê placa em
tempo real — tira **uma** foto em vez de até doze, e desiste em ~2 s se a câmera não
entregar quadro (contra 20 s do modo normal).

**A resposta tem exatamente as mesmas chaves.** Não há um segundo formato para tratar.

| | Normal | `rapido=1` |
|---|---|---|
| Fotos | até 12 | 1–2 |
| Teto do laço | 28 s | 5 s |
| Espera pela câmera | 20 s | 2 s |
| Reforço de OCR p/ placa borrada | sim | não |
| Varredura em janelas (moto de longe) | sim | não |

**O que se perde é específico, não genérico:** placa borrada e moto distante. São os dois
casos que os recursos desligados existem para resolver — e por isso o modo é escolha de
quem chama, uma chamada de cada vez, e não uma configuração global.

**`confirmada` continua sendo o portão, com o mesmo limiar.** O modo rápido não afrouxa
critério nenhum: ele lê menos, e isso aparece como mais `confirmada: false` e mais
`placa: null`. Continue tratando `false` como "mandar para conferência".

O campo **`modo`** na resposta (`"rapido"` ou `"completo"`) diz qual perfil atendeu. Se
você pediu `rapido=1` e voltou `"completo"`, o modo está desligado no servidor (há um
interruptor em Configuração) e o aviso aparece em `avisos` — a chamada rodou completa e
pode ter levado os 30 s de sempre.

Timeout HTTP recomendado nessas chamadas: **~10 s**.

## Tempo de resposta

A chamada **pode levar até ~30 segundos** — o servidor tira várias fotos até a leitura
ficar confiável ou o tempo esgotar. Configure o timeout HTTP do cliente Java para
**pelo menos 35-40 segundos**

A consulta de dados do veículo **não muda essa recomendação**, e não é para somar nada a
ela: o servidor só consulta dentro do tempo que sobrou do orçamento da leitura, e desiste
em vez de estourá-lo. Na prática ela custa **0 segundo** na maioria das chamadas (a placa
já está em cache) e no máximo ~2,5s na primeira vez que aquela placa aparece. As leituras
que consomem os ~30 segundos inteiros são justamente as que não fecham consenso — e essas
**não** disparam consulta nenhuma., e trate a chamada como bloqueante/assíncrona conforme o
resto do fluxo do roteador exigir (não deve travar a liberação da bomba, por exemplo).

## Autenticação

Nenhuma por padrão — o endpoint é público, pensado para rede interna do posto.

Cada posto pode opcionalmente ganhar uma **chave própria** (gerada em `/empresas` →
"API/LGPD", no painel do posto): quando isso é feito, as chamadas com o CNPJ daquele
posto passam a exigir o cabeçalho `X-API-Key` (ou `?api_key=...` na própria URL) com o
valor gerado — postos sem chave própria continuam públicos normalmente. Confirme com
quem administra o servidor se o posto que você está integrando tem chave configurada
antes de simular a chamada.

Há também uma api_key GLOBAL do servidor inteiro (`config.txt`), separada desta — ela
protege o painel administrativo, não `/api/leitura`.

## Testando sem esperar um abastecimento de verdade

Cada posto tem, na tela dele (`/posto/{id}`), um botão **"Testar como o roteador"** —
ele monta e dispara essa mesma chamada com os dados já cadastrados e mostra a URL
usada. Serve para validar a integração sem precisar simular um abastecimento real.
Em bico de duas câmeras, o resultado mostra o quadro de cada uma, marcando qual leu a
placa e qual não detectou nada — é por ali que se ajusta o enquadramento.
