# Plano de Reorganização de Arquivos do ALPR

> **Status: executado (Fases 0 a 3 concluídas e validadas ao vivo).** Mantido como registro
> da migração. Duas correções em relação ao plano original: `ambiente.py` e `hardware.py`
> (não listados na varredura inicial) também foram movidos para `app/visao/`; e o split do
> `ocr.py` (Fase 2) preservou os métodos da classe `OCR` intactos, porque a divisão foi por
> classe/função de nível de módulo (`engines.py`/`auto.py`), não por método interno, para
> não alterar lógica numa pipeline em produção sem suíte de testes.
>
> Objetivo original: sair de 19 módulos soltos na raiz para um pacote `app/` em camadas,
> sem quebrar o comportamento em runtime.

---

## 0. Princípio que guia todo o plano

O código usa **importação absoluta plana** (`import banco`) e **caminhos relativos ao
CWD** (`Path("placas.db")`, `directory="templates"`, `Path("models")`).

Consequência prática:

- **Imports** quebram ao mover para pacotes → precisam ser reescritos.
- **Caminhos de asset/runtime** só continuam funcionando se o app for executado
  **a partir da raiz do repositório** e se `templates/`, `static/`, `models/`,
  `hls/`, `fonte/`, `config.txt`, `placas.db`, `testes/` **permanecerem na raiz**.

➡️ **Decisão-chave:** o código Python vai para `app/`, mas os diretórios de
asset e de dados **ficam na raiz**. Assim evitamos reescrever ~40 referências de
caminho. Mover `templates/`/`static/` para dentro de `app/web/` fica como fase
opcional futura (exige alterar cada `directory=...` e cada `Path(...)`).

---

## 1. Estrutura-alvo

```
leitura-placas/
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── servidor.py
│   ├── core/            config.py  estado.py  banco.py  broadcaster.py
│   ├── visao/           camera.py  detector.py  validador.py  tracker.py  pipeline.py
│   │   └── ocr/         __init__.py  engines.py  preprocess.py  auto.py   (fase 2)
│   ├── streaming/       stream.py  hls_encoder.py
│   ├── operacao/        supervisor.py  dns_server.py
│   ├── seguranca/       sessao.py            (ex-auth.py)
│   └── web/             api.py auth.py paginas.py stream.py testes.py __init__.py  (ex-rotas/)
│
├── benchmarks/          benchmark_stream.py  benchmark_tracker.py
├── docs/                ARQUITETURA.md  CASOS_DE_USO.md  OTIMIZACAO.md  IDENTIDADE_VISUAL.md  este arquivo
├── scripts/             (inalterado)
├── testes/              (inalterado — fica na raiz por causa dos caminhos)
├── templates/  static/  models/  fonte/  hls/   (assets — ficam na raiz)
├── config.txt  placas.db                        (runtime — ficam na raiz)
├── README.md  requirements.txt  pyproject.toml  Dockerfile  docker-compose.yml  entrypoint.sh
```

Regra de dependência (verificável depois): `core` não importa ninguém de domínio;
`visao`/`streaming`/`operacao`/`seguranca` importam só `core`; `web` e `servidor`
importam de todos; nada importa `web` a não ser `servidor`.

---

## 2. Mapa de movimentação de arquivos

| De (raiz) | Para |
|-----------|------|
| `main.py` | `app/main.py` |
| `servidor.py` | `app/servidor.py` |
| `config.py` | `app/core/config.py` |
| `estado.py` | `app/core/estado.py` |
| `banco.py` | `app/core/banco.py` |
| `broadcaster.py` | `app/core/broadcaster.py` |
| `auth.py` | `app/seguranca/sessao.py`  **(renomeado)** |
| `camera.py` | `app/visao/camera.py` |
| `detector.py` | `app/visao/detector.py` |
| `validador.py` | `app/visao/validador.py` |
| `tracker.py` | `app/visao/tracker.py` |
| `pipeline.py` | `app/visao/pipeline.py` |
| `ocr.py` | `app/visao/ocr/` (fase 2, ver §6) |
| `stream.py` | `app/streaming/stream.py` |
| `hls_encoder.py` | `app/streaming/hls_encoder.py` |
| `supervisor.py` | `app/operacao/supervisor.py` |
| `dns_server.py` | `app/operacao/dns_server.py` |
| `rotas/*.py` | `app/web/*.py` |
| `benchmark_*.py` | `benchmarks/` |
| `*.md` (exceto README) | `docs/` |

Adicionar `__init__.py` (vazio) em: `app/`, `core/`, `visao/`, `streaming/`,
`operacao/`, `seguranca/`, `web/`.

---

## 3. Mapa de reescrita de imports

Substituir em cada arquivo (imports internos apenas, libs externas intactas):

| Import antigo | Import novo |
|---------------|-------------|
| `import config` | `from app.core import config` |
| `import estado` | `from app.core import estado` |
| `import banco` | `from app.core import banco` |
| `import broadcaster as bc` | `from app.core import broadcaster as bc` |
| `import auth as auth_mod` | `from app.seguranca import sessao as auth_mod` |
| `from camera import Camera` | `from app.visao.camera import Camera` |
| `from detector import Detector` | `from app.visao.detector import Detector` |
| `from ocr import OCR` | `from app.visao.ocr import OCR` |
| `from validador import validar` | `from app.visao.validador import validar` |
| `import tracker` | `from app.visao import tracker` |
| `import pipeline` | `from app.visao import pipeline` |
| `import camera as camera_mod` | `from app.visao import camera as camera_mod` |
| `import stream as stream_mod` | `from app.streaming import stream as stream_mod` |
| `import hls_encoder as hls_mod` | `from app.streaming import hls_encoder as hls_mod` |
| `import supervisor as sv` | `from app.operacao import supervisor as sv` |
| `import dns_server as dns_mod` | `from app.operacao import dns_server as dns_mod` |
| `import servidor` | `from app import servidor` |
| `from rotas import api, paginas` | `from app.web import api, paginas` |
| `from rotas import auth as auth_rotas` | `from app.web import auth as auth_rotas` |
| `from rotas import stream as stream_rotas` | `from app.web import stream as stream_rotas` |
| `from rotas import testes as testes_rotas` | `from app.web import testes as testes_rotas` |

### Arquivos afetados por import (checklist)

- `app/main.py` → 1 import (`servidor`)
- `app/servidor.py` → ~13 imports (é o mais afetado)
- `app/visao/pipeline.py` → banco, broadcaster, estado, camera, detector, ocr, validador, tracker
- `app/visao/ocr/…` → estado
- `app/visao/tracker.py` → (só boxmot opcional, sem interno)
- `app/streaming/stream.py`, `app/streaming/hls_encoder.py` → estado
- `app/operacao/supervisor.py` → pipeline, estado
- `app/web/api.py` → banco, camera, config, estado, pipeline, supervisor
- `app/web/auth.py` → sessao (ex-auth), banco
- `app/web/paginas.py` → config
- `app/web/stream.py` → stream (streaming)
- `app/web/testes.py` → config, banco, estado, pipeline, camera (imports lazy dentro de funções, atenção às linhas 152/166/179-183)

> **Não** alterar strings de caminho (`directory="templates"`, `Path("models")`,
> `Path("static/snapshots")`, `Path("placas.db")`, etc.), pois elas continuam válidas
> porque o CWD permanece a raiz. Só mexer nelas na fase opcional §7.

---

## 4. Ajustes fora do Python

| Arquivo | O que muda |
|---------|-----------|
| `entrypoint.sh` | comando de start passa a `python -m app.main` (era `python main.py` / `servidor`). Verificar a linha do `exec`. |
| `Dockerfile` | `CMD`/`ENTRYPOINT` idem; garantir `WORKDIR /app-root` (raiz) e que o `COPY` traga `app/` + assets na raiz. **Cuidado:** se o `WORKDIR` já for `/app`, renomear para evitar colisão com o pacote `app/`. |
| `docker-compose.yml` | conferir `command:` se houver. |
| `pyproject.toml` (novo) | declara pacote `app`, permite `python -m app.main` e futura instalação. Ver §8. |
| `.gitignore` | conferir se `config.txt`/`placas.db`/`hls/` continuam ignorados (paths não mudam). |
| `docs/ARQUITETURA.md` | atualizar §17 (diagrama de dependências) e nomes de arquivo. |

Ponto de atenção do `main.py`: hoje faz `import servidor`. Como vira `app/main.py`,
rodar `python app/main.py` **não** funciona (imports absolutos `app.*` exigem a raiz
no path). Padronizar a execução em **`python -m app.main`** a partir da raiz.

---

## 5. Ordem de execução recomendada (fases pequenas, testáveis)

Cada fase termina com o servidor subindo (`python -m app.main` ou atual) e uma
navegação rápida (`/`, `/dashboard`, um stream, `/api/status`).

1. **Fase 0, sem risco de import:**
   - Criar `benchmarks/` e mover `benchmark_*.py`.
   - Criar `docs/` e mover os `.md`.
   - Nada de import de produção muda. Validar que benchmarks ainda rodam (eles
     fazem `sys.path` próprio, então conferir linhas iniciais).

2. **Fase 1, criar o pacote e mover em bloco:**
   - Criar `app/` + subpastas + `__init__.py`.
   - `git mv` (ou mover) todos os módulos conforme §2, `rotas/`→`app/web/`,
     `auth.py`→`app/seguranca/sessao.py`.
   - Reescrever **todos** os imports conforme §3, de uma vez.
   - Ajustar `entrypoint.sh`/`Dockerfile`/compose (§4).
   - Adicionar `pyproject.toml`.
   - Subir e validar tudo. Esta é a fase grande; fazer num commit isolado.

3. **Fase 2, quebrar `ocr.py`** (independente, ver §6).

4. **Fase 3 (opcional), mover `templates/`/`static/` para `app/web/`** (§7).

> Alternativa de menor risco à Fase 1: manter *shims* temporários na raiz
> (`banco.py` contendo `from app.core.banco import *`) para migrar consumidores aos
> poucos. Só vale a pena se a migração for espalhada em vários dias.

---

## 6. Quebra do `ocr.py` (904 linhas) na Fase 2

Transformar `ocr.py` no pacote `app/visao/ocr/` preservando a API pública `OCR.ler()`:

- `ocr/__init__.py`: expõe a classe `OCR` (fachada). `from app.visao.ocr import OCR`
  continua funcionando.
- `ocr/engines.py`: inicialização e chamada de cada engine (tesseract, easyocr,
  paddleocr, doctr, fast_plate_ocr) atrás de uma interface comum.
- `ocr/preprocess.py`: `_remover_header`, `_remover_ruidos_mercosul`,
  `_focar_caracteres` e utilitários de imagem.
- `ocr/auto.py`: a lógica de seleção automática de engine (§6 do ARQUITETURA.md).

Fazer **só depois** da Fase 1 estar estável, e num commit próprio, porque envolve
recortar código (não só mover). Manter `estado.registrar_crop_ocr` sendo chamado do
mesmo ponto.

---

## 7. Mover assets para dentro de `app/` na Fase 3 (opcional)

Só se quiser um pacote 100% autocontido. Exige, para cada caminho relativo:

- Definir uma âncora de projeto, ex. em `app/core/config.py`:
  `RAIZ = Path(__file__).resolve().parents[2]` e derivar
  `TEMPLATES_DIR = RAIZ / "app" / "web" / "templates"`, etc.
- Trocar `directory="templates"` → `directory=str(TEMPLATES_DIR)` em
  `paginas.py`/`auth.py`/`servidor.py`.
- Trocar `Path("models")`, `Path("static/snapshots")`, `Path("placas.db")`,
  `Path("hls")`, `Path("testes/...")` por caminhos ancorados em `RAIZ`.
- Reavaliar `entrypoint.sh` (ele cria `config.txt` em `/app/config.txt`).

Não recomendado junto com a Fase 1, porque dobra o risco por pouco ganho.

---

## 8. `pyproject.toml` mínimo sugerido

```toml
[project]
name = "leitura-placas"
version = "0.1.0"
requires-python = ">=3.10"
# dependências continuam em requirements.txt por ora

[tool.setuptools.packages.find]
include = ["app*"]
```

Execução padrão após a migração: `python -m app.main` (a partir da raiz do repo).

---

## 9. Checklist de validação pós-migração

- [ ] `python -m app.main` sobe sem ImportError.
- [ ] Páginas: `/`, `/dashboard`, `/historico`, `/cameras`, `/roi/{id}`, `/login` renderizam (templates encontrados).
- [ ] Streams: `/stream.mjpg`, `/stream/{id}.mjpg`, `/snapshot.jpg` respondem.
- [ ] API: `/api/status`, `/api/health`, `/api/deteccoes`, `/api/config`.
- [ ] Assets estáticos (`/static/...`) e snapshots carregam.
- [ ] `models/*.onnx` é encontrado pelo detector; OCR inicializa.
- [ ] `config.txt` e `placas.db` continuam sendo lidos/gravados na raiz.
- [ ] Build do Docker sobe e o container serve na porta 14000.
- [ ] Benchmarks em `benchmarks/` ainda executam.
- [ ] Atualizar `docs/ARQUITETURA.md` §17 e §2 com os novos caminhos.
```
