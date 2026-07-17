"""Leitura/gravação do config.txt em formato `chave = valor`."""
from __future__ import annotations
import os
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "config.txt"))

PADROES: dict[str, str] = {
    "porta": "14000",
    # Câmera: tipo = usb | csi | rtsp | intelbras
    "camera_tipo": "intelbras",
    "camera_indice": "0",
    "camera_largura": "1280",
    "camera_altura": "720",
    "camera_fps": "15",
    # Configuração Intelbras (linha VIP — protocolo RTSP/Dahua)
    "intelbras_host": "192.168.1.108",
    "intelbras_porta": "554",
    "intelbras_usuario": "admin",
    "intelbras_senha": "",
    "intelbras_canal": "1",
    "intelbras_subtype": "1",          # 0 = main stream, 1 = sub stream (recomendado para ALPR no Pi)
    "intelbras_formato": "padrao",     # padrao | legado (VIP 1120/1220/1130)
    "rtsp_transporte": "tcp",          # tcp | udp — transporte RTSP (tcp = mais estável)
    # Backend de detecção:
    #   open_image_models = YOLOv9-t via open-image-models (licença MIT, uso comercial OK)
    #   onnx              = modelo ONNX local em modelo_path (suporta votação MultiDetector)
    "detector_backend": "open_image_models",
    # Modelo open-image-models (input size ↓ = mais rápido no Pi, ↑ = mais preciso)
    # yolo-v9-t-256/384/416/512/640-license-plate-end2end · yolo-v9-s-608-license-plate-end2end
    # 512 = melhor equilíbrio (detecta placas inclinadas/difíceis a ~152ms). Para máxima
    # precisão use yolo-v9-s-608 (mais lento); para Pi lento, 416 (mais rápido).
    "oim_modelo": "yolo-v9-t-512-license-plate-end2end",
    # Modelo usado só na LEITURA sob demanda (botão "Ler Placa" / GET). Como esse fluxo
    # tolera mais latência, usa o modelo mais preciso (s-608: 87.5% no UFPR-ALPR real).
    # O stream ao vivo continua no oim_modelo (512, mais rápido).
    "oim_modelo_leitura": "yolo-v9-s-608-license-plate-end2end",
    "modelo_path": "models/plate_detector.onnx",
    # Modelos ONNX extras para votação (separados por vírgula, ex: "models/yolov8s.onnx")
    "detector_modelos_extra": "",
    # Mínimo de modelos que precisam detectar a mesma região para aceitar (1 = desativado)
    "detector_votos_minimos": "1",
    "conf_threshold": "0.3",
    "nms_threshold": "0.4",
    # ocr_engine: auto | tesseract | easyocr | paddleocr | doctr | fast_plate_ocr
    # auto = detecta formato pela faixa colorida: Mercosul→fast_plate_ocr, Antigo→easyocr
    # engines não instalados são instalados automaticamente via pip na primeira inicialização
    "ocr_engine": "auto",
    # Engines extras para votação (separados por vírgula, ex: "easyocr,fast_plate_ocr")
    # Vazio = usa somente ocr_engine (comportamento anterior)
    "ocr_engines_extra": "",
    # Votos mínimos para aceitar uma leitura (1 = desativado, 2 = exige ≥2 engines concordando)
    "ocr_votos_minimos": "1",
    # Número de fotos tiradas por clique em "Ler Placa" — resultado eleito por votação
    "snapshots_votacao": "3",
    # Máximo de detecções YOLO+OCR por segundo. Reduzir alivia CPU sem afetar o stream ao vivo.
    "deteccao_fps_max": "5",
    "tesseract_psm": "6",
    "frames_consenso": "3",
    "cooldown_seg": "120",
    # Performance: roda YOLO+OCR somente a cada N frames capturados.
    # 1 = todo frame (mais carga). 2-3 = recomendado para CPU.
    # Stream continua exibindo todo frame, so a deteccao e' que pula.
    "processar_a_cada_n_frames": "2",
    # sim = detecta placas continuamente no stream
    # nao = stream ativo mas detecção só pelo botão "Ler Placa"
    "deteccao_automatica": "sim",
    "salvar_snapshot": "sim",
    "snapshot_qualidade": "85",
    "alerta_lista_negra": "sim",
    "webhook_todas": "nao",       # sim = dispara webhook para TODA placa detectada
    "webhook_url": "",
    "log_level": "info",
    "implantado": "nao",
    "api_key": "",   # chave opcional para acesso à API sem cookie de sessão
    # ByteTrack: rastreamento de veículos entre frames para reduzir chamadas OCR
    # Requer: pip install boxmot  (fallback automático para modo clássico se não instalado)
    "tracker_ativo": "sim",
    "tracker_ocr_intervalo": "5",   # roda OCR a cada N frames do mesmo veículo rastreado
    "tracker_votos_emitir": "2",    # leituras OCR concordantes para emitir a placa
    # ── Ajuste adaptativo de imagem (brilho/contraste/saturação por ambiente) ──
    # Analisa cada frame, classifica a cena (noite/baixa_luz/nublado/sol_forte/normal)
    # e corrige a imagem antes da detecção — melhora a captura em condições ruins.
    "ajuste_ambiente": "sim",          # sim = ativa o ajuste adaptativo
    "ajuste_brilho_alvo": "120",       # luminância média alvo (0-255) para o gamma automático
    "ajuste_forca": "0.8",             # intensidade do ajuste (0.0 = nada, 1.0 = total)
    "ajuste_clahe": "sim",             # contraste local adaptativo (bom p/ neblina/chuva)
    "ajuste_wb": "sim",                # balanço de branco gray-world (remove dominante de cor)
    "ajuste_saturacao": "sim",         # compensa saturação conforme a cena
    "ajuste_denoise_noite": "sim",     # redução de ruído leve à noite/baixa luz
    "ajuste_recalc_frames": "8",       # reclassifica a cena a cada N frames (custo/estabilidade)
    # mjpeg = stream independente por viewer (simples, sem deps)
    # hls   = encode único → N viewers sem custo adicional (requer ffmpeg no PATH)
    "streaming_modo": "mjpeg",
    # DNS local embutido — resolve o hostname abaixo para o IP deste servidor
    # Linux: sudo setcap 'cap_net_bind_service=+ep' $(which python3)
    "dns_ativo": "nao",
    "dns_nome": "lpr.redesoft",
    "dns_upstream": "8.8.8.8",
}


def carregar() -> dict[str, str]:
    cfg = dict(PADROES)
    if CONFIG_PATH.exists():
        for linha in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            chave, _, valor = linha.partition("=")
            cfg[chave.strip()] = valor.strip()
    for chave in cfg:
        env_key = chave.upper()
        if env_key in os.environ:
            cfg[chave] = os.environ[env_key]
    return cfg


def salvar(cfg: dict[str, str]) -> None:
    linhas = [f"{k} = {v}" for k, v in cfg.items()]
    CONFIG_PATH.write_text("\n".join(linhas) + "\n", encoding="utf-8")


def get_int(cfg: dict[str, str], chave: str) -> int:
    return int(cfg.get(chave, PADROES.get(chave, "0")))


def get_float(cfg: dict[str, str], chave: str) -> float:
    return float(cfg.get(chave, PADROES.get(chave, "0")))


def get_bool(cfg: dict[str, str], chave: str) -> bool:
    return cfg.get(chave, "").strip().lower() in ("sim", "true", "1", "yes")
