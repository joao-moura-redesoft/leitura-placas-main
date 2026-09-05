# Runbook da feira — operar a vitrine `/feira`

Checklist de quem vai ficar no estande. O modo feira em si está documentado no código
(`app/visao/feira.py` explica *por que* o mock existe e o que ele deliberadamente não faz);
aqui é só o **procedimento do dia**, na ordem em que ele acontece.

O estado atual da máquina de demonstração já está armado: posto `Posto de Demonstração`
(empresa 4) → automação 3 → bico 6 → câmera 7 (USB, índice 0), placa `MOK3H92`.

---

## 1. Antes de abrir o estande (10 min)

| # | Passo | Como confirmar |
|---|---|---|
| 1 | Subir o servidor | `ALPR.exe` (ou `python -m app.main`). A porta é a **14000**. |
| 2 | Fazer login **no próprio kiosk** | `/feira` exige sessão. Sem login ele manda para `/login`. |
| 3 | Abrir `/feira` e deixar em tela cheia | Botão de tela cheia na barra superior. |
| 4 | **Apontar a câmera para a placa** | Ver seção 2 — é o passo que mais dá errado. |
| 5 | Fazer uma leitura de ensaio | Botão ↻ ("Forçar leitura"). Tem de aparecer o card "Bem-vindo!". |

> **O login é obrigatório e é fácil de esquecer.** A vitrine não é pública: se alguém
> reiniciar a máquina de manhã e só abrir `/feira`, cai na tela de login na frente do
> público. Faça o login *antes* de o estande abrir.

---

## 2. Enquadramento — o ponto crítico

**Este é o item que decide se a demonstração funciona.** Em 05/09 a câmera de demonstração
estava sobre uma mesa apontada para um monitor e cabos: em 19 tentativas de OCR ela não leu
nada, porque não havia placa nenhuma no quadro.

Como saber que o enquadramento está certo, sem adivinhar:

1. Ligue a câmera ao vivo no kiosk (botão de câmera na barra superior).
2. A placa tem de aparecer **de frente e ocupando largura**, não de canto.
3. Confira no log (`alpr.log`) uma linha de OCR e olhe o `aspect`:

```
OCR crop=118x37px aspect=3.19 ... -> RLR2H45 conf=0.76    <- BOM (placa deitada, ~3 a 4)
OCR crop=111x352px aspect=0.32 ... -> NADA                <- RUIM (recorte em pé: não é placa)
```

`aspect` entre **3 e 4** é uma placa de verdade. `aspect` abaixo de 1 quer dizer que o
detector travou num objeto vertical (quina de monitor, tomada, poste) e **nenhum ajuste de
OCR vai salvar** — o que precisa mudar é para onde a câmera aponta.

Um jeito rápido de ver o que a câmera vê, sem depender da tela:

```bash
ls -t app/web/static/snapshots/*cam7-amostra.jpg | head -1     # abra esse arquivo
```

---

## 3. Durante a demonstração

- **Fluxo normal:** o carro chega, a vitrine lê sozinha (varre a cada ~1,6 s) e mostra o card.
- **Se não fechar sozinho:** aperte **↻** (forçar leitura). Por padrão usa o perfil
  **completo** — mais fotos, mais robusto e mais lento. Dá para trocar para **rápido** em
  Configuração › Modo feira (ver seção 5) se a espera pesar mais que a acurácia:

  | Perfil do botão ↻ | Fotos | Orçamento | Quando usar |
  |---|---|---|---|
  | **Completo** (padrão) | até 12 | 28 s | Ângulo difícil; vale esperar para fechar a leitura |
  | **Rápido** | até 2 | 5 s | Estande com fila e câmera bem enquadrada |

  O **loop automático usa sempre o rápido**, independente dessa configuração.
- **Para o próximo visitante:** botão "Próximo veículo", ou **Espaço / Enter / Esc**, ou um
  clique em qualquer lugar do card. O card **não** sai sozinho — isso é de propósito.
- **Sair da vitrine:** o "×" pede **dois cliques** (o primeiro só arma). É proteção contra
  encerrar a demo com um toque errado.

### Visitante querendo testar a placa do próprio celular

Funciona, e **não** vira o carro de demonstração: o mock só casa com `MOK3H92` (tolerância
de 2 caracteres) e só no posto de demonstração. Uma placa qualquer fica a uma distância
grande e segue o caminho real. O que ele verá é a leitura verdadeira — sem o card de ficha,
porque só há ficha cadastrada para o carro da demo.

---

## 4. Mensagens da tela e o que fazer

| O que aparece | Significa | O que fazer |
|---|---|---|
| "Nenhuma placa reconhecida — reposicione o veículo" | O OCR não fechou | Reposicione; se repetir, **volte à seção 2** (é enquadramento) |
| "Sessão expirada — recarregue a página (F5)" | A sessão caiu (kiosk parado > 1 h) | **F5** e refaça o login |
| "A demonstração precisa de ajuste no cadastro" | Bico/câmera fora do cadastro | Configuração › seção Modo feira |
| "Sem conexão com o servidor da demonstração" | O servidor caiu ou a rede sumiu | Ver se o `ALPR.exe` ainda está de pé |
| "Muitas leituras seguidas" | Freio de 45 leituras/min | Nada — ele volta sozinho |
| Fica em "Aguardando veículo" para sempre | Provavelmente sem bico configurado | Configuração › seção Modo feira |

---

## 5. Chegar na configuração do modo feira

A seção é **oculta de propósito** (é uma tela de bastidor, e o estande é público):

1. Vá em **Configuração**.
2. Dê **7 cliques rápidos no título "Configuração"** (menos de 1,2 s entre um e outro).
3. A seção "Modo feira (demonstração)" aparece, em destaque amarelo.

Ela fica lembrada **só até fechar o navegador** (`sessionStorage`). Reabriu o Chrome, refaça
o gesto — não é defeito.

Ali dá para: criar/remover o posto de demonstração, trocar a câmera (USB ou rede), editar as
**fichas** (modelo, cor, ano, combustível e a mensagem do card), escolher a **captura do
botão ↻** (completo ou rápido — ver seção 3) e ligar/desligar o modo.

---

## 6. Sem internet — o que continua funcionando

O estande foi pensado para operar **offline**, e a configuração da máquina já está assim
(`apiplacas_ativo = nao`, sem webhook, sem DNS):

- A leitura da placa é **100 % local** (detector + OCR em ONNX na própria máquina).
- Os dados do veículo do card (combustível, modelo, cor, ano) vêm da **ficha local**
  (`feira_fichas.json`), não da internet.
- O bloco `veiculo` do payload do roteador também sai completo, montado da ficha e marcado
  com `origem="feira"`.

Ou seja: **não depende do wi-fi da feira.**

---

## 7. Se travar no meio do evento

Em ordem, do mais barato para o mais caro:

1. **F5 na página.** Resolve sessão expirada e loop parado.
2. **Botão ↻.** Força uma leitura com o perfil completo.
3. **Desligar/ligar a câmera ao vivo** no botão da barra — reabre o stream.
4. **Reiniciar o `ALPR.exe`.** Sobe em segundos; nada do cadastro se perde.
5. **Último recurso:** o card do carro de demonstração aparece com o mock, então basta a
   câmera enxergar a placa *de qualquer jeito* — não precisa de leitura perfeita.

---

## 8. O que NÃO fazer

- **Não** desligue o modo feira para "testar" durante o evento: sem `feira_empresa_id` a
  vitrine deixa de existir e `/feira` passa a redirecionar.
- **Não** apague a pasta `app/web/static/snapshots/` com o servidor rodando.
- **Não** apresente a leitura do carro de demonstração como número de acurácia: ela é
  **mockada de propósito** e fica fora da métrica (`origem="feira"`). Para falar de precisão,
  use a leitura real de um veículo qualquer.
