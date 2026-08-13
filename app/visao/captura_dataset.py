"""Captura de imagens para o DATASET de testes — não para o histórico.

O pipeline só grava snapshot quando a leitura dá certo: detecção, OCR, validação,
consenso e cooldown, todos passando (`pipeline._emitir`). Isso é o certo para o
histórico, mas produz um dataset que só contém o que o sistema JÁ acerta — e portanto
inútil para medir onde ele falha.

O caso concreto é moto. Em 12/08/2026 o operador revisou 74 capturas automáticas e não
encontrou UMA moto, enquanto o dataset seguia com 2. Não é azar: se a placa de moto não
é detectada, ou é detectada e não é lida, nenhum snapshot é gravado. A captura movida a
sucesso é cega exatamente para o que precisa ser medido.

Este módulo grava o que o outro caminho descarta, em dois gatilhos:

  negativo  — o detector achou uma caixa e a leitura falhou. Barato e dirigido: é
              onde cai a placa suja, cortada ou pequena demais.
  amostra   — o quadro inteiro, de tempos em tempos, INDEPENDENTE de detecção. É o
              único gatilho que pega moto cuja placa nem chega a ser detectada, que é
              a hipótese mais provável hoje no pipeline ao vivo.

As imagens vão para a mesma pasta dos snapshots, então a fila de classificação em
/testes as pega sozinha. Os nomes são propositalmente impossíveis de confundir com
placa (`_placa_do_nome` exige 7 alfanuméricos seguidos de ponto): quem classificar
digita a placa olhando a imagem, que é o que se quer aqui — não existe leitura do OCR
para sugerir.

Desligado por padrão. Ligar custa disco e enche a fila de classificação de imagens sem
nada acontecendo nelas; vale quando se está montando base de propósito.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2

log = logging.getLogger(__name__)

SNAPSHOT_DIR = Path("app/web/static/snapshots")

_coletores: list[threading.Thread] = []
_parar = threading.Event()


def _sim(valor) -> bool:
    return str(valor).strip().lower() in ("sim", "true", "1", "yes")


class CapturaDataset:
    """Um por câmera. Guarda os relógios dos gatilhos e respeita o teto de disco."""

    def __init__(self, cfg: dict, camera_db_id: int):
        self.camera_db_id = camera_db_id
        self.ativo = _sim(cfg.get("captura_dataset", "nao"))
        self.negativos = _sim(cfg.get("captura_dataset_negativos", "sim"))
        self.intervalo = float(cfg.get("captura_dataset_intervalo_seg", "60"))
        # Intervalo próprio para negativo: sem ele, uma caixa fantasma fixa na cena
        # (um adesivo, uma placa de sinalização) gravaria a cada tick de detecção.
        self.intervalo_neg = float(cfg.get("captura_dataset_negativo_intervalo_seg", "20"))
        self.qualidade = int(cfg.get("snapshot_qualidade", "85"))
        # Teto de arquivos: a captura periódica é continua, e sem limite ela enche o
        # disco de um servidor que tambem grava video. Ao bater o teto ela PARA, em vez
        # de apagar: apagar arriscaria remover snapshot referenciado por uma detecção.
        self.max_arquivos = int(cfg.get("captura_dataset_max_arquivos", "5000"))
        self._ultima_amostra = 0.0
        self._ultimo_negativo = 0.0
        self._avisou_teto = False

    # ── gatilhos ──────────────────────────────────────────────────────────────
    def amostrar(self, frame) -> None:
        """Quadro inteiro, de tempos em tempos, aconteça o que acontecer na cena."""
        if not self.ativo or frame is None:
            return
        agora = time.time()
        if agora - self._ultima_amostra < self.intervalo:
            return
        if self._salvar(frame, "amostra"):
            self._ultima_amostra = agora

    def negativo(self, crop) -> None:
        """Recorte que o detector achou e a leitura não conseguiu resolver."""
        if not self.ativo or not self.negativos or crop is None or crop.size == 0:
            return
        agora = time.time()
        if agora - self._ultimo_negativo < self.intervalo_neg:
            return
        if self._salvar(crop, "naolido"):
            self._ultimo_negativo = agora

    # ── escrita ───────────────────────────────────────────────────────────────
    def _cabe_no_disco(self) -> bool:
        if self.max_arquivos <= 0:
            return True
        try:
            n = sum(1 for f in SNAPSHOT_DIR.iterdir() if f.suffix.lower() in (".jpg", ".png"))
        except OSError:
            return True
        if n < self.max_arquivos:
            self._avisou_teto = False
            return True
        if not self._avisou_teto:
            log.warning("Captura para dataset PARADA: %d imagens em %s atingem o teto de "
                        "%d (captura_dataset_max_arquivos). Classifique ou limpe a pasta.",
                        n, SNAPSHOT_DIR, self.max_arquivos)
            self._avisou_teto = True
        return False

    def salvar_amostra_agora(self, frame) -> bool:
        """Grava sem consultar o relógio — quem chama já controlou a cadência."""
        return self._salvar(frame, "amostra") if self.ativo else False

    def _salvar(self, img, marca: str) -> bool:
        if not self._cabe_no_disco():
            return False
        try:
            SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            # Milissegundos, nao segundos: dois gatilhos no mesmo segundo geravam o MESMO
            # nome e o segundo sobrescrevia o primeiro em silencio. Aconteceu no log de
            # 13/08/2026 (20260813T164002_cam6-amostra.jpg gravado 13:40:02 e 13:40:03) —
            # uma amostra da fila de classificacao perdida sem nenhum aviso.
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3]
            # O hifen e o que impede `_placa_do_nome` de ler isto como placa: ele exige
            # 7 alfanumericos seguidos de ponto.
            nome = f"{ts}_cam{self.camera_db_id}-{marca}.jpg"
            cv2.imwrite(str(SNAPSHOT_DIR / nome), img,
                        [int(cv2.IMWRITE_JPEG_QUALITY), self.qualidade])
            log.debug("Captura para dataset: %s", nome)
            return True
        except Exception as e:
            # Nunca pode derrubar o pipeline: isto e coleta acessoria, nao operacao.
            log.warning("Falha ao gravar captura para dataset (%s): %s", marca, e)
            return False


# ── Coletor autônomo ──────────────────────────────────────────────────────────────
# Os gatilhos acima vivem dentro do Pipeline, que só roda com `deteccao_automatica=sim`.
# Quando a detecção contínua está desligada — que é o caso comum, porque a leitura é
# reativa ao bico — não existe laço nenhum olhando a câmera, e nada seria coletado.
#
# Ligar `deteccao_automatica` só para coletar sairia caro: mantém RTSP aberto, roda OCR
# sem parar e grava detecções `pipeline` que aparecem no histórico de produção. Para
# juntar imagens não é preciso nada disso. Este coletor abre a câmera, pega UM quadro,
# fecha, e dorme — sem detector, sem OCR, sem banco.

def _coletar_de_camera(cam_id: int, intervalo: float) -> None:
    from app.core import banco
    from app.core import config as cfg_mod
    from app.visao import camera as camera_mod
    from app.visao.leitura import lock_camera

    while not _parar.wait(intervalo):
        try:
            cfg = cfg_mod.carregar()
            cap = CapturaDataset(cfg, cam_id)
            if not cap.ativo:            # desligado no config sem reiniciar: para de gravar
                continue
            cam = banco.cameras_obter(cam_id)
            if not cam or not cam.get("ativo", 1):
                continue
            # O mesmo lock da leitura reativa: uma câmera, uma conexão RTSP por vez.
            # Sem isto a coleta disputaria o stream com a leitura de um bico.
            with lock_camera(cam_id):
                frame = camera_mod.capturar_frame_unico(
                    tipo=cam["camera_tipo"],
                    indice=cam.get("rtsp_url_custom") or cam.get("camera_indice", "0"),
                    largura=int(cfg.get("camera_largura", "1280")),
                    altura=int(cfg.get("camera_altura", "720")),
                    fps=int(cfg.get("camera_fps", "15")),
                    intelbras={
                        "host": "" if cam.get("rtsp_url_custom") else cam.get("intelbras_host", ""),
                        "porta": cam.get("intelbras_porta", "554"),
                        "usuario": cam.get("intelbras_usuario", "admin"),
                        "senha": cam.get("intelbras_senha") or cfg.get("intelbras_senha", ""),
                        "canal": cam.get("intelbras_canal", "1"),
                        "subtype": cam.get("intelbras_subtype", "1"),
                        "formato": cam.get("intelbras_formato", "padrao"),
                        "rtsp_transporte": cfg.get("rtsp_transporte", "tcp"),
                    },
                    # Abrir e fechar RTSP é o trabalho normal deste laço, não um evento:
                    # em INFO eram 2 linhas por câmera a cada volta, sem nada a dizer.
                    silencioso=True,
                )
            if frame is not None:
                cap.salvar_amostra_agora(frame)
        except Exception as e:
            # Câmera fora do ar não pode matar a thread: na próxima volta tenta de novo.
            log.warning("Coletor de dataset (câmera %d): %s", cam_id, e)


def iniciar_coletor(cfg: dict) -> int:
    """Sobe uma thread por câmera ativa. Devolve quantas subiram."""
    if not _sim(cfg.get("captura_dataset", "nao")):
        return 0
    from app.core import banco

    intervalo = float(cfg.get("captura_dataset_intervalo_seg", "60"))
    _parar.clear()
    n = 0
    for cam in banco.cameras_listar():
        if not cam.get("ativo", 1):
            continue
        t = threading.Thread(target=_coletar_de_camera, args=(cam["id"], intervalo),
                             daemon=True, name=f"coletor-dataset-{cam['id']}")
        t.start()
        _coletores.append(t)
        n += 1
    if n:
        log.info("Coletor para dataset ativo: %d câmera(s), 1 quadro a cada %.0fs", n, intervalo)
    return n


def parar_coletor() -> None:
    _parar.set()
