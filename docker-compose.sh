#!/bin/sh
# Wrapper pra rodar o compose sem esquecer o override de GPU. docker-compose.gpu.yml
# NÃO roda sozinho (não tem porta/volumes/healthcheck, só o pedaço de deploy.resources
# de GPU) e digitar os dois -f toda vez é fácil de errar. Este script detecta a GPU
# NVIDIA no host e escolhe os arquivos certos sozinho.
#
# Uso (qualquer subcomando do docker compose passa direto):
#   ./docker-compose.sh up -d --build
#   ./docker-compose.sh down
#   ./docker-compose.sh logs -f
#   ./docker-compose.sh ps
#
# Forçar um modo específico (ex.: testar a imagem CPU numa máquina com GPU):
#   ./docker-compose.sh --cpu up -d --build
#   ./docker-compose.sh --gpu up -d --build
set -e

cd "$(dirname "$0")"

case "$1" in
  --gpu) MODO=gpu; shift ;;
  --cpu) MODO=cpu; shift ;;
  *)
    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
      MODO=gpu
    else
      MODO=cpu
    fi
    ;;
esac

if [ "$MODO" = gpu ]; then
  echo "[docker-compose.sh] GPU NVIDIA detectada — usando docker-compose.yml + docker-compose.gpu.yml"
  exec docker compose -f docker-compose.yml -f docker-compose.gpu.yml "$@"
else
  echo "[docker-compose.sh] Sem GPU — usando docker-compose.yml (CPU)"
  exec docker compose -f docker-compose.yml "$@"
fi
