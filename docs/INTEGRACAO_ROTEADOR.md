# Integração com o roteador do posto (leitura reativa de placa)

Especificação para quem vai desenvolver a chamada HTTP do lado do posto (sidecar
Java/roteador) contra o servidor central de leitura de placas.

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
  "votos_snapshot": 5, "total_snapshots": 6,
  "votos_ocr": 2, "total_engines": 2,
  "detalhes_ocr": [{"engine": "fast_plate_ocr", "placa": "PGK2D93", "padrao": "mercosul", "confianca": 0.91}],
  "tentativas": 6,
  "parada_motivo": "acordo",
  "snapshot": "/static/snapshots/20260721T185912_PGK2D93.jpg",
  "frame_url": "/static/snapshots/preview_bico_2.jpg"
}
```
HTTP `200`. Use o campo `"placa"`. `"acordo"` (0 a 1) é a confiança do consenso interno
— abaixo de ~0.6 vale tratar como "leitura duvidosa" em vez de aceitar cegamente.
Os demais campos (`votos_ocr`, `detalhes_ocr`, etc.) são detalhe interno de diagnóstico;
não é necessário processá-los.

### Sucesso na chamada, mas nenhuma placa encontrada

```json
{
  "placa": null, "mensagem": "Nenhuma placa detectada nos frames",
  "camera_id": 3, "bico_id": 2,
  "snapshots_analisados": 12, "tentativas": 12, "parada_motivo": "timeout",
  "frame_url": "/static/snapshots/preview_bico_2.jpg"
}
```
Também HTTP `200`. `"placa": null` é uma resposta válida — sem carro na área, placa
suja, ou tempo esgotado sem conseguir ler. Não é erro de integração.

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

### Câmera não respondeu

```json
{"detail": "Não foi possível conectar à câmera via RTSP (...). Verifique o IP/host, porta, usuário e senha."}
```
HTTP `503`. Problema de infraestrutura do posto (câmera offline, rede), não de
integração.

## Tempo de resposta

A chamada **pode levar até ~30 segundos** — o servidor tira várias fotos até a leitura
ficar confiável ou o tempo esgotar. Configure o timeout HTTP do cliente Java para
**pelo menos 35-40 segundos**, e trate a chamada como bloqueante/assíncrona conforme o
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
