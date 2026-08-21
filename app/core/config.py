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
    # ── Detecção em 2 estágios (veículo → placa) ────────────────────────────────
    # Detecta o veículo primeiro (YOLOX-s ONNX, COCO, licença Apache-2.0) e busca a
    # placa só dentro dele — elimina falsos positivos fora de veículo (texto de fundo,
    # placas de exemplo em telas) e melhora placa pequena/distante. Fallback SEGURO: se
    # nenhum veículo for achado, cai para a busca no frame inteiro (comportamento atual).
    "veiculo_dois_estagios_get": "sim",     # leitura GET ("Ler Placa") — tolera a latência
    "veiculo_dois_estagios_live": "nao",    # stream ao vivo — desligado por padrão (latência)
    "veiculo_modelo_path": "models/vehicle_detector.onnx",
    # 0,25 e nao 0,4 (medido em 20/08/2026 nos quadros reais salvos): a 0,40 a
    # cobertura de tipo era 85% e a 0,25 vai a 92%, sem NENHUM veredito mudando ou se
    # perdendo. NAO baixe mais: a 0,15 a mediana de veiculos/quadro chega a 5 e 42% dos
    # quadros estouram `veiculo_max_veiculos`, cujo desempate mantem os MAIORES — as
    # motos sao expulsas do top-5 e a contagem de moto foi a ZERO na medicao.
    "veiculo_conf": "0.25",
    "veiculo_nms": "0.5",
    "veiculo_classes": "2,3,5,7",           # COCO: car, motorcycle, bus, truck
    "veiculo_padding": "0.05",              # margem ao redor do veículo antes de buscar a placa
    "veiculo_obrigatorio": "nao",           # sim = estrito (sem veículo → sem placa, sem fallback)
    # Limita quantos veículos (maiores primeiro) disparam busca de placa por frame — protege
    # a latência em cenas movimentadas (estacionamento/rua com dezenas de veículos visíveis).
    "veiculo_max_veiculos": "5",
    # ── Varredura em janelas (fallback de último recurso da leitura GET) ────────
    # Quando a passada normal não acha NENHUMA placa no recorte, reexamina o recorte em
    # janelas sobrepostas de ~`tiles_lado_alvo` px. Recupera placa de MOTO parada na bomba:
    # medido em cena real, uma placa de 38px numa ROI de 397x610 não sai em passada única
    # (nem ampliando a ROI — o modelo redimensiona de volta para 608), mas sai com conf
    # 0.4-0.8 numa janela de ~250x300 que pegue moto+placa juntas. Ver `BuscaEmTiles`.
    # Só no GET: custa até `tiles_max_janelas` passadas extras (~200ms cada em CPU), e
    # apenas nas leituras que teriam falhado de todo jeito.
    "tiles_fallback_get": "sim",
    "tiles_lado_alvo": "300",         # lado alvo da janela, em px do recorte analisado
    # Faixa útil ESTREITA e por motivo geométrico: sobreposição maior = janela maior, e em
    # 0.5 a janela já é quase o recorte inteiro (o enquadramento que falhou). Medido:
    # 0.25-0.35 acham a placa da moto, 0.20/0.40/0.50 não acham nada. Não mexa sem medir.
    "tiles_sobreposicao": "0.30",
    "tiles_max_janelas": "6",         # teto de janelas por tentativa (protege a latência)
    # Limiar de confiança só nas janelas — mais permissivo que `conf_threshold` de propósito:
    # nas janelas a placa de moto sai raspando (0.19-0.37 medido) e aqui já se sabe que o
    # caminho normal falhou. Recorte ruim ainda tem que passar por OCR + validar() + consenso
    # entre frames; medido na cena real, nenhum falso positivo até 0.10.
    "tiles_conf": "0.15",
    # ocr_engine: auto | tesseract | easyocr | paddleocr | doctr | fast_plate_ocr
    # auto = detecta formato pela faixa colorida: Mercosul→fast_plate_ocr, Antigo→easyocr
    # engines não instalados são instalados automaticamente via pip na primeira inicialização
    "ocr_engine": "auto",
    # Reforço PaddleOCR (Apache-2.0) na leitura GET ("Ler Placa"): melhora muito placa
    # antiga borrada (UFPR-ALPR: 49%→64%). Só atua em linha única e quando o crop está
    # borrado (nítido usa AutoOCR). Vale só no GET (tolera a latência maior). ocr_engine=auto.
    "ocr_leitura_paddle": "sim",
    # Engines extras para votação (separados por vírgula, ex: "easyocr,fast_plate_ocr")
    # Vazio = usa somente ocr_engine (comportamento anterior)
    "ocr_engines_extra": "",
    # Votos mínimos para aceitar uma leitura (1 = desativado, 2 = exige ≥2 engines concordando)
    "ocr_votos_minimos": "1",
    # ── Deskew (correção de rotação) ───────────────────────────────────────────
    # Corrige inclinação rotacional da placa antes do OCR via minAreaRect + warpAffine.
    # Custo: ~1–3 ms por crop. Com tracker ativo, impacto global desprezível.
    "deskew_ativo": "sim",              # sim = ativa correção de rotação antes do OCR
    "deskew_angulo_max": "30",          # ângulo máximo permitido para correção (graus)
    # ── Loop de leitura por confiança (botão "Ler Placa" / GET) ────────────────────
    # Em vez de tirar um número fixo de fotos e votar uma vez, tira fotos incrementalmente
    # e para assim que o CONSENSO ficar forte o bastante — padrão "reject-retry" de ALPR.
    # Número MÍNIMO de fotos antes de permitir parada antecipada (evita parar num acerto
    # isolado de sorte). Resultado eleito por votação por caractere entre as leituras.
    "snapshots_votacao": "3",
    # Máximo de fotos tiradas antes de desistir e responder com o que tiver (limite superior).
    "leitura_max_tentativas": "12",
    # Tempo máximo (segundos) do loop de leitura, mesmo que o máximo de fotos não seja
    # atingido. Medido em CPU (dev, sem GPU): ~7s/tentativa em crop pequeno/borrado com
    # 2 estágios + s-608 + ensemble PaddleOCR — 6s não dava nem para 1 tentativa completa,
    # e menos ainda para atingir o mínimo de fotos antes de poder parar por acordo. Em
    # produção (GPU), cada tentativa deve ser bem mais rápida — este é um teto de
    # segurança, não um alvo (o loop já para antes via `leitura_acordo_minimo`).
    "leitura_timeout_seg": "28",
    # Concordância mínima (0-1) entre as leituras para parar antecipadamente com confiança.
    # 0.80 = para assim que 80%+ do peso das leituras concordar com a placa eleita.
    "leitura_acordo_minimo": "0.80",
    # Máximo de detecções YOLO+OCR por segundo — e também a frequência do ajuste de
    # ambiente e da publicação de frame (stream/HLS/ler-placa), que andam junto com a
    # detecção (ver comentário em app/visao/pipeline.py:_loop). Reduzir alivia CPU bastante.
    "deteccao_fps_max": "5",
    "tesseract_psm": "6",
    "frames_consenso": "3",
    "cooldown_seg": "120",
    # sim = detecta placas continuamente no stream
    # nao = stream ativo mas detecção só pelo botão "Ler Placa"
    "deteccao_automatica": "sim",
    "salvar_snapshot": "sim",
    # Guarda também o QUADRO INTEIRO de cada detecção (com a marcação da placa), além do
    # recorte. É o que permite conferir depois se pegou o veículo certo e se a área do
    # bico está bem posicionada. Custa ~200x mais disco que o recorte (~150KB contra ~1KB),
    # por isso tem interruptor próprio.
    "salvar_frame_deteccao": "sim",
    "snapshot_qualidade": "85",

    # ── Coleta para o DATASET de testes (app/visao/captura_dataset.py) ──────────────
    # `salvar_snapshot` acima só grava LEITURA BEM-SUCEDIDA, então a base cresce contendo
    # apenas o que o sistema já acerta. Isto grava o que ele erra, que é o que falta para
    # medir: o quadro inteiro de tempos em tempos (pega até a moto cuja placa nem chega a
    # ser detectada) e o recorte que o detector achou e a leitura não resolveu.
    # Desligado por padrão: custa disco e enche a fila de classificação de /testes.
    "captura_dataset": "nao",
    "captura_dataset_negativos": "sim",          # vale só quando captura_dataset=sim
    "captura_dataset_intervalo_seg": "60",       # entre amostras do quadro inteiro
    "captura_dataset_negativo_intervalo_seg": "20",
    # Teto de imagens na pasta de snapshots. Ao bater, a coleta PARA (não apaga: apagar
    # arriscaria remover snapshot que uma detecção do histórico referencia). 0 = sem teto.
    "captura_dataset_max_arquivos": "5000",
    # Dias que `deteccoes`/`chamadas` (e os JPEGs de snapshot/frame) ficam guardados antes
    # de serem apagados automaticamente. 0 = nunca apaga (crescimento ilimitado — cuidado
    # num servidor multi-tenant de longa duração). Ajuste conforme a política de retenção
    # de dados/imagens do cliente (ex.: LGPD, se as imagens contêm veículo/placa de terceiros).
    "retencao_dias": "90",
    "alerta_lista_negra": "sim",
    "webhook_todas": "nao",       # sim = dispara webhook para TODA placa detectada
    "webhook_url": "",
    "log_level": "info",
    "implantado": "nao",
    "api_key": "",   # chave opcional para acesso à API sem cookie de sessão
    # sim = o cookie de sessão só é enviado em HTTPS (flag `Secure`). Desligado por
    # padrão porque quebraria o acesso local padrão (http://localhost:14000, primeiro
    # boot); ligue quando o servidor estiver atrás de um proxy reverso com TLS.
    "cookie_secure": "nao",
    # ── E-mail (opcional) — "esqueci minha senha" e convite de usuário novo ────
    # Vazio (smtp_host="") = recurso desligado: sem servidor de e-mail configurado,
    # essas duas telas caem no aviso "peça a um administrador" em vez de quebrar.
    "smtp_host": "",
    "smtp_porta": "587",
    "smtp_usuario": "",
    "smtp_senha": "",
    "smtp_remetente": "",              # vazio = usa smtp_usuario
    "smtp_tls": "sim",
    # Base pra montar o link nos e-mails (ex.: "https://alpr.suaempresa.com"). Vazio =
    # usa o host da própria requisição que disparou o e-mail — funciona pra a maioria
    # dos casos, mas atrás de proxy reverso o host visto pelo servidor pode não ser o
    # público; preencha se os links saírem errados.
    "url_base": "",
    # ByteTrack: rastreamento de veículos entre frames para reduzir chamadas OCR
    # Requer: pip install boxmot  (fallback automático para modo clássico se não instalado)
    "tracker_ativo": "sim",
    "tracker_ocr_intervalo": "5",   # roda OCR a cada N frames do mesmo veículo rastreado
    "tracker_votos_emitir": "2",    # leituras OCR concordantes para emitir a placa
    # Frames de detecção tolerados sem casar o veículo antes de considerá-lo perdido.
    # Baixo demais fragmenta um veículo parado na bomba (oclusão momentânea) em vários
    # IDs de track — cada um vota do zero e pode emitir uma placa levemente diferente
    # pro mesmo carro, duplicando a linha no histórico.
    "tracker_paciencia_frames": "40",
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
    # Reclassifica a cena a cada N ticks de DETECÇÃO (não mais a cada N frames de
    # câmera — o ajuste de ambiente passou a rodar junto com a detecção, ver
    # app/visao/pipeline.py:_loop). Antes, 8 a 15 fps = ~0,5s de atraso pra reagir a
    # uma mudança de cena (entardecer, veículo entrando na sombra). Se o valor
    # continuasse 8 agora que a cadência é a de `deteccao_fps_max` (tipicamente 5/s),
    # o atraso triplicaria para ~1,6s; 3 mantém a mesma dinâmica de antes (~0,6s).
    "ajuste_recalc_frames": "3",
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
