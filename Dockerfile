# Imagem CPU. Para o servidor de produção com GPU NVIDIA use o Dockerfile.gpu
# (docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d) — sem ele o
# onnxruntime/torch caem para CPU silenciosamente e a leitura fica em ~7s por tentativa.
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-por \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    ffmpeg \
    v4l-utils \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Usuário sem privilégio: um RCE no processo não vira root no container. UID/GID fixos
# em 1000 porque os volumes são bind mounts do host — o dono das pastas no host precisa
# bater com este UID (ver README/compose: `sudo chown -R 1000:1000 dados models snapshots`).
RUN groupadd --gid 1000 alpr && useradd --uid 1000 --gid 1000 --create-home alpr

WORKDIR /app

# requirements primeiro, em camada própria: mudar código da aplicação não invalida o
# cache do pip (que baixa torch/paddle/easyocr — a parte lenta do build).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# O que entra aqui é filtrado pelo .dockerignore (sem .venv/.git/config.txt/placas.db).
COPY --chown=alpr:alpr . .

# Diretórios de escrita em runtime que ficam DENTRO de /app (não são volumes montados):
#   - hls/                       segmentos HLS (só quando streaming_modo=hls)
#   - testes/fotos, .../crops    saídas da aba de testes (o .dockerignore os exclui)
# O app cria essas pastas via os.makedirs no boot, mas roda como alpr (UID 1000) e /app
# pertence ao root (WORKDIR cria /app como root; o --chown acima só afeta o CONTEÚDO
# copiado). Sem criá-las aqui, com dono alpr, o boot morre com "Permission denied" ao
# tentar criar /app/hls. Criadas e chowneadas no build resolve de vez.
RUN mkdir -p /app/hls /app/testes/fotos /app/testes/resultados/crops \
    && chown -R alpr:alpr /app/hls /app/testes

# `COPY` + `RUN chmod` (em vez de `COPY --chmod`) porque --chmod exige BuildKit; assim a
# imagem também constrói com o builder clássico. O bit de execução não sobrevive ao
# checkout em Windows, por isso o chmod.
RUN cp /app/entrypoint.sh /entrypoint.sh && chmod 755 /entrypoint.sh

# HOME define onde os modelos são baixados em runtime (~/.cache/open-image-models,
# ~/.EasyOCR, ~/.paddleocr). No compose isso é um volume nomeado — sem ele, cada
# recriação do container rebaixa centenas de MB de modelo.
ENV HOME=/home/alpr \
    DB_PATH=/app/dados/placas.db \
    CONFIG_PATH=/app/dados/config.txt

# Sem `USER alpr` aqui de propósito: o entrypoint precisa de root para preparar os
# volumes (que o Docker cria como root no primeiro `up`) e só então baixa o privilégio
# com setpriv. O SERVIDOR roda como alpr — confirme com `docker compose exec alpr id`.
# Fixar USER aqui devolveria o passo manual de `chown` no host antes do primeiro deploy.

EXPOSE 14000

# Liveness também na imagem (vale para `docker run` avulso, sem compose). Endpoint
# público e sem dado de cliente — ver app/web/api.py:healthz.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD curl -sf http://localhost:14000/api/healthz || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "app.main"]
