#!/bin/sh
# Roda como root só para preparar volumes e baixar o modelo que falta; o servidor em si
# é executado como o usuário sem privilégio `alpr` (UID 1000) pelo setpriv no final.
#
# Por que root aqui: os volumes chegam vazios no primeiro `up` e o Docker os cria como
# root. Um container que já começasse como UID 1000 não conseguiria escrever neles e o
# deploy exigiria um `chown` manual no host antes de qualquer coisa — justamente o
# "passo a mais" que este entrypoint elimina.
set -e

UID_APP=1000
GID_APP=1000

DADOS_DIR="$(dirname "${CONFIG_PATH:-/app/dados/config.txt}")"
SNAPSHOTS_DIR=/app/app/web/static/snapshots
MODELS_DIR=/app/models
VEICULO_ONNX="$MODELS_DIR/vehicle_detector.onnx"

mkdir -p "$DADOS_DIR" "$MODELS_DIR" "$SNAPSHOTS_DIR"

# ── Detector de veículo (1º estágio de veiculo_dois_estagios_get=sim, ligado por padrão)
# 36 MB, licença Apache-2.0, repositório PÚBLICO do HuggingFace (sem credencial). Não é
# versionado no git nem embutido na imagem. Sem ele o sistema não quebra: cai para busca
# no frame inteiro, com apenas um WARNING no log — degradação silenciosa que custa
# precisão. Baixamos aqui para o deploy não depender de um passo manual.
# Best-effort: se não houver internet, segue o boot normalmente (só avisa).
if [ ! -f "$VEICULO_ONNX" ]; then
  echo "[entrypoint] Baixando detector de veículo (~36 MB, uma única vez)..."
  python - "$VEICULO_ONNX" <<'PY' || echo "[entrypoint] AVISO: download falhou — a detecção em 2 estágios vai operar em fallback (frame inteiro), com menos precisão. Rode 'python scripts/baixar_modelo.py --veiculo' depois."
import sys, pathlib, requests

destino = pathlib.Path(sys.argv[1])
url = ("https://huggingface.co/opencv/object_detection_yolox/resolve/main/"
       "object_detection_yolox_2022nov.onnx")
parcial = destino.with_suffix(".onnx.parcial")
with requests.get(url, stream=True, timeout=120) as r:
    r.raise_for_status()
    with open(parcial, "wb") as f:
        for pedaco in r.iter_content(chunk_size=1 << 20):
            f.write(pedaco)
# Renomeia só no fim: um download interrompido não deixa um .onnx truncado para trás,
# que o VehicleDetector tentaria carregar e falharia de um jeito bem mais confuso.
parcial.rename(destino)
print(f"[entrypoint] Modelo salvo: {destino} ({destino.stat().st_size // 1024} KB)")
PY
fi

# ── config.txt inicial ────────────────────────────────────────────────────────────
# Gerado a partir de app/core/config.py (PADROES), a fonte de verdade — a lista fixa que
# existia aqui antes ficou desatualizada (fixava ocr_engine=tesseract e um modelo que não
# é mais o backend padrão), e toda chave nova do projeto nascia faltando.
if [ ! -f "$CONFIG_PATH" ]; then
  python -c "from app.core import config; config.salvar(config.PADROES)"
  echo "[entrypoint] config.txt criado em $CONFIG_PATH a partir dos padrões do projeto."
fi

# O banco não é pré-criado com `touch`: banco.inicializar() cria arquivo e schema. Um
# arquivo de 0 byte só atrapalhava o diagnóstico quando o volume estava montado errado
# (parecia banco existente, mas vazio).

# ── Permissões ────────────────────────────────────────────────────────────────────
# `chown -R` só quando o diretório ainda não pertence ao app: em snapshots com milhares
# de imagens, um -R incondicional atrasaria todo restart.
for d in "$DADOS_DIR" "$MODELS_DIR" "$SNAPSHOTS_DIR"; do
  if [ "$(stat -c %u "$d")" != "$UID_APP" ]; then
    chown -R "$UID_APP:$GID_APP" "$d"
  fi
done

echo "[entrypoint] Iniciando como usuário alpr (UID $UID_APP)."
# setpriv em vez de gosu/su-exec: vem do util-linux, já presente nas duas imagens base —
# sem pacote extra. `exec` mantém o processo como PID 1 do container, para o SIGTERM do
# `docker stop` chegar ao servidor e o shutdown do FastAPI rodar.
#
# --clear-groups (não --init-groups): --init-groups exige resolver o UID para um nome no
# /etc/passwd para descobrir os grupos suplementares, e essa resolução falha em algumas
# imagens ("--init-groups requires an user that can be found on the system"). Aqui o
# usuário só tem o próprio grupo primário (1000), então não há grupo suplementar a
# inicializar. --clear-groups usa os IDs numéricos direto, sem lookup, e ainda descarta
# os grupos do root em vez de herdá-los — mais seguro.
exec setpriv --reuid="$UID_APP" --regid="$GID_APP" --clear-groups "$@"
