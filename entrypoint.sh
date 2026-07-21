#!/bin/sh
set -e

if [ ! -f /app/placas.db ]; then
  touch /app/placas.db
fi

if [ ! -f /app/config.txt ]; then
  cat > /app/config.txt <<'EOF'
porta = 14000
camera_tipo = intelbras
intelbras_host = 192.168.1.108
intelbras_porta = 554
intelbras_usuario = admin
intelbras_senha =
intelbras_canal = 1
intelbras_subtype = 1
intelbras_formato = padrao
camera_largura = 1280
camera_altura = 720
camera_fps = 15
modelo_path = models/plate_detector.onnx
conf_threshold = 0.5
nms_threshold = 0.4
ocr_engine = tesseract
tesseract_psm = 7
frames_consenso = 3
cooldown_seg = 30
salvar_snapshot = sim
snapshot_qualidade = 85
alerta_lista_negra = sim
webhook_url =
log_level = info
EOF
  echo "[entrypoint] config.txt criado — edite intelbras_host/senha antes do primeiro uso"
fi

mkdir -p /app/models /app/app/web/static/snapshots

exec "$@"
