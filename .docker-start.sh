#!/bin/sh
# Sobe o servidor via Docker Compose com um comando só, escolhendo CPU ou GPU sozinho.
#
#     ./.docker-start.sh            # autodetecta GPU (nvidia-smi) e usa o stack certo
#     ./.docker-start.sh --cpu      # força CPU mesmo com GPU disponível
#     ./.docker-start.sh --gpu      # força GPU (falha cedo se faltar o NVIDIA Container Toolkit)
#     ./.docker-start.sh --logs     # depois de subir, anexa em `docker compose logs -f`
#
# Sem argumento nenhum é o caminho de produção descrito no README: sem passo manual antes
# nem depois, exceto abrir http://localhost:14000 e criar o primeiro administrador.
set -e
cd "$(dirname "$0")"

MODO=auto
SEGUIR_LOGS=no
for arg in "$@"; do
  case "$arg" in
    --cpu)  MODO=cpu ;;
    --gpu)  MODO=gpu ;;
    --logs) SEGUIR_LOGS=sim ;;
    *) echo "Argumento desconhecido: $arg (use --cpu, --gpu ou --logs)" >&2; exit 1 ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker não encontrado no PATH — instale o Docker Engine/Desktop antes de continuar." >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "'docker compose' (plugin v2) não encontrado — atualize o Docker." >&2
  exit 1
fi

# Autodetecção: só olha se o comando nvidia-smi existe, não se a GPU está de fato utilizável
# (isso o próprio `docker-compose.gpu.yml` já valida — falha alto se faltar o NVIDIA
# Container Toolkit, de propósito, em vez de cair pra CPU em silêncio).
if [ "$MODO" = "auto" ]; then
  if command -v nvidia-smi >/dev/null 2>&1; then
    MODO=gpu
  else
    MODO=cpu
  fi
fi

if [ "$MODO" = "gpu" ]; then
  echo "[.docker-start] Modo GPU — docker-compose.yml + docker-compose.gpu.yml"
  ARQUIVOS="-f docker-compose.yml -f docker-compose.gpu.yml"
else
  echo "[.docker-start] Modo CPU — docker-compose.yml"
  ARQUIVOS="-f docker-compose.yml"
fi

docker compose $ARQUIVOS up -d --build

cat <<EOF

Subindo. O boot carrega detector + OCR (45-90s em CPU, mais rápido em GPU) antes do
healthcheck ficar "healthy" — acompanhe com:

    docker compose ps
    docker compose logs -f alpr

Quando estiver no ar: http://localhost:14000 (crie o primeiro administrador pela tela).
EOF

if [ "$SEGUIR_LOGS" = "sim" ]; then
  exec docker compose logs -f alpr
fi
