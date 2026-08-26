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

# Sufixos que ESTE modulo cria (ver `_salvar`: `{ts}_cam{N}-{marca}.jpg`). Sao a fronteira
# entre "arquivo meu" e "arquivo de outro subsistema", e essa fronteira e o que torna a
# evicção segura - ver `_cabe_no_disco`.
#
# `naolido-moto` existe porque moto e o caso raro que o throttle compartilhado apagava da
# estatistica: um negativo a cada 20 s, com carro inundando o gatilho, faz a moto chegar
# sempre no instante em que o relogio acabou de disparar. Marca propria + cota propria e o
# que a torna achavel por nome em vez de estatisticamente invisivel.
MARCAS = ("amostra", "naolido", "naolido-moto")
SUFIXOS_MEUS = tuple("-%s.jpg" % m for m in MARCAS)

# Quantos segundos o inventario de arquivos vale antes de ser recontado. `_cabe_no_disco`
# roda em TODO gatilho de captura, e listar 5.000 arquivos a cada 10 s e I/O jogado fora
# num servidor que tambem grava video.
_TTL_INVENTARIO_SEG = 30.0

# Folga que a evicção abre ALEM do excedente, para nao ser apagar-um-gravar-um em cada
# gatilho (cada um custaria uma listagem da pasta).
#
# Teto ABSOLUTO era bug: com `_LOTE_EVICCAO = 50` e um teto de 10, `excedente + 50` apagava
# os 10 arquivos e a pasta ficava vazia. A folga tem de ser proporcional ao teto — 1% dele —
# com o absoluto virando so o limite superior. Teto 5.000 da folga 50 (1%); teto 10 da 1.
_FOLGA_EVICCAO_MAX = 50
_FRACAO_FOLGA = 100          # 1/100 do teto


def _folga_eviccao(teto: int) -> int:
    return max(1, min(_FOLGA_EVICCAO_MAX, teto // _FRACAO_FOLGA))


def _meus_arquivos() -> list[Path]:
    """So os arquivos que ESTE modulo criou, do mais antigo para o mais novo.

    Ordena pelo NOME e nao por mtime: o nome comeca com o timestamp UTC em milissegundos
    (`_salvar`), e isso e estavel. `mtime` muda se alguem copiar ou tocar o arquivo, e um
    backup restaurado reordenaria a fila de evicção inteira.
    """
    try:
        return sorted((f for f in SNAPSHOT_DIR.iterdir()
                       if f.name.endswith(SUFIXOS_MEUS)), key=lambda f: f.name)
    except OSError:
        return []


def _rotulados() -> set[str]:
    """Nomes de arquivo que o dataset referencia - a evicção nunca pode toca-los.

    Le `testes/dataset.json` porque rotulo humano e a coisa mais caro de reproduzir neste
    projeto: apagar uma captura ja rotulada transformaria trabalho de gente numa linha
    apontando para arquivo inexistente, que e um modo de falha que este projeto JA teve
    (commit 2252896, "Corrige o caminho das capturas no dataset e tira arquivo faltando da
    acuracia").

    Hoje nenhuma entrada do dataset e `-amostra`/`-naolido` (as 48 que vivem em snapshots/
    sao recortes de deteccao, que a evicção nao alcanca de qualquer forma). A checagem
    existe para o dia em que alguem rotular uma captura - e ai o custo de nao ter checado
    seria silencioso.

    Falha em ler = conjunto vazio seria o mais perigoso possivel, entao devolve None e quem
    chama ABORTA a evicção. Nao apagar nada e sempre recuperavel; apagar rotulo nao e.
    """
    import json
    try:
        dados = json.loads(Path("testes/dataset.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        # Sem dataset nao ha rotulo a proteger - mas AVISA, porque "arquivo ausente" e
        # ambiguo: pode ser projeto novo (legitimo) ou processo rodando do diretorio errado
        # (e ai a evicção correria sem protecao nenhuma). O caminho e relativo de proposito,
        # a mesma convencao de `SNAPSHOT_DIR`, e o Dockerfile leva `testes/dataset.json`
        # (o .dockerignore exclui so `fotos/` e `resultados/`).
        log.warning("Evicção sem protecao de rotulo: testes/dataset.json nao encontrado a "
                    "partir de %s", Path.cwd())
        return set()
    except (OSError, ValueError) as e:
        log.warning("Evicção abortada: nao consegui ler o dataset (%s)", e)
        return None
    return {Path(f["arquivo"]).name for f in dados.get("fotos", []) if f.get("arquivo")}

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
        self.intervalo = float(cfg.get("captura_dataset_intervalo_seg", "300"))
        # Intervalo próprio para negativo: sem ele, uma caixa fantasma fixa na cena
        # (um adesivo, uma placa de sinalização) gravaria a cada tick de detecção.
        self.intervalo_neg = float(cfg.get("captura_dataset_negativo_intervalo_seg", "300"))
        # Cota SEPARADA para moto. Sem ela a moto nunca era coletada mesmo com a captura
        # ligada: um so relogio de negativo, com carro inundando o gatilho, faz a moto
        # chegar sempre logo depois de ele disparar. 60 s e mais permissivo que o negativo
        # comum de proposito - moto e o caso escasso, e o que se quer e justamente nao
        # perde-la quando ela aparece.
        self.intervalo_neg_moto = float(cfg.get("captura_dataset_moto_intervalo_seg", "60"))
        self.qualidade = int(cfg.get("snapshot_qualidade", "85"))
        # Teto de arquivos: a captura periódica é continua, e sem limite ela enche o disco de
        # um servidor que tambem grava video.
        #
        # O comentario original aqui dizia "ao bater o teto ela PARA, em vez de apagar:
        # apagar arriscaria remover snapshot referenciado por uma deteccao". O raciocinio
        # estava certo e o risco era real - o que mudou e que a evicção agora so alcanca os
        # arquivos que ESTE modulo criou (`SUFIXOS_MEUS`), e esses nunca sao referenciados
        # por `deteccoes`. O risco foi removido, nao ignorado.
        #
        # Parar era pior do que parecia: medido em 25/08/2026, a captura estava desligada ha
        # 12 dias e nenhuma moto podia ser coletada nesse periodo. Ver `_cabe_no_disco`.
        self.max_arquivos = int(cfg.get("captura_dataset_max_arquivos", "5000"))
        self._ultima_amostra = 0.0
        self._ultimo_negativo = 0.0
        self._ultimo_negativo_moto = 0.0
        self._avisou_teto = False
        # Inventario em cache (contagem, instante) - ver `_TTL_INVENTARIO_SEG`.
        self._inventario = (0, 0.0)

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

    def negativo(self, crop, tipo_veiculo: str | None = None) -> None:
        """Recorte que o detector achou e a leitura nao conseguiu resolver.

        `tipo_veiculo` ('moto'/'carro'/None) vem da classe do detector de veiculo e escolhe
        qual relogio governa a gravacao. Moto tem o seu, e isso e o ponto: com UM relogio
        compartilhado, a moto - que e o caso raro - chegava quase sempre no instante em que o
        negativo de carro acabou de disparar, e era descartada. Doze dias de captura ligada
        em agosto/2026 produziram 1.045 negativos e a revisao humana nao achou UMA moto neles.
        Nao era azar de amostra: era o throttle.

        `None` (o 2 estagios nao rodou, ou nao achou veiculo contendo a placa) cai no relogio
        comum de proposito. Chutar 'moto' no desconhecido daria cota de caso raro para o caso
        comum, que e o oposto do que se quer - e `tipo_veiculo` e None em 423 das 838
        deteccoes do banco, entao o chute governaria a maioria.
        """
        if not self.ativo or not self.negativos or crop is None or crop.size == 0:
            return
        agora = time.time()
        e_moto = tipo_veiculo == "moto"
        ultimo = self._ultimo_negativo_moto if e_moto else self._ultimo_negativo
        intervalo = self.intervalo_neg_moto if e_moto else self.intervalo_neg
        if agora - ultimo < intervalo:
            return
        if self._salvar(crop, "naolido-moto" if e_moto else "naolido"):
            if e_moto:
                self._ultimo_negativo_moto = agora
            else:
                self._ultimo_negativo = agora

    # ── escrita ───────────────────────────────────────────────────────────────
    def _cabe_no_disco(self) -> bool:
        """Ha vaga para mais uma captura? Se nao, abre uma apagando a MAIS ANTIGA.

        Duas mudancas em 25/08/2026, e as duas vieram de medicao no posto:

        1. Conta so os arquivos que ESTE modulo criou. Antes contava todo `.jpg` da pasta, e
           tres subsistemas escrevem nela: alem daqui, `leitura.py` e `pipeline.py` gravam o
           HISTORICO (`{ts}_{placa}.jpg`, referenciado por `deteccoes.snapshot`). Medido: 4.184
           arquivos meus contra 1.594 de historico, num teto de 5.000 - o historico consumia
           cota do dataset, e como ele cresce a cada leitura bem-sucedida e nao pode ser
           apagado sem quebrar o link, o teto virava uma catraca que desligava a captura para
           sempre. A mensagem antiga pedia "classifique ou limpe a pasta", e classificar nao
           mudava a contagem.

        2. Ao encher, EVICTA em vez de parar. Parar custou 12 dias sem coleta nenhuma (nada
           entre 13/08 e 25/08), e nesse periodo nenhuma moto podia ser coletada - o que
           bloqueava a unica pendencia que precisa de amostra nova. Com 349 arquivos/hora
           medidos, qualquer teto e um relogio: corrigir so a contagem daria 2,3 horas de
           coleta antes de travar de novo.

        A evicção e segura porque so alcanca `SUFIXOS_MEUS`, que nunca aparecem em
        `deteccoes.snapshot`, e porque pula o que o dataset referencia (`_rotulados`).
        """
        if self.max_arquivos <= 0:
            return True

        agora = time.time()
        n, medido_em = self._inventario
        if agora - medido_em > _TTL_INVENTARIO_SEG:
            n = len(_meus_arquivos())
            medido_em = agora

        if n < self.max_arquivos:
            self._avisou_teto = False
            # Conta a que vai ser gravada agora, para nao relistar a pasta a cada gatilho.
            # `medido_em` NAO avanca: o TTL tem de expirar a partir da ultima LISTAGEM, senao
            # a contagem incrementada nunca seria confrontada com o disco e a deriva (gravacao
            # que falhou, arquivo apagado por fora) se acumularia sem nunca ser corrigida.
            self._inventario = (n + 1, medido_em)
            return True

        # Cheio: abre vaga. Recontagem forcada - o cache pode estar velho e listar de novo
        # e barato ao lado de apagar por engano.
        meus = _meus_arquivos()
        self._inventario = (len(meus), agora)
        if len(meus) < self.max_arquivos:
            return True

        protegidos = _rotulados()
        if protegidos is None:
            # Nao consegui ler o dataset: PARA, como antes. Nao apagar e recuperavel;
            # apagar rotulo humano nao e.
            if not self._avisou_teto:
                log.warning("Captura pausada: teto de %d atingido e o dataset esta ilegivel, "
                            "entao a evicção nao roda (risco de apagar rotulo).",
                            self.max_arquivos)
                self._avisou_teto = True
            return False

        # Quantos apagar: o excedente mais uma folga proporcional ao teto — ver
        # `_folga_eviccao`, e o bug de teto absoluto que ela conserta.
        alvo = len(meus) - self.max_arquivos + _folga_eviccao(self.max_arquivos)
        apagados = 0
        for f in meus:                     # `_meus_arquivos` ja vem do mais antigo
            if apagados >= alvo:
                break
            if f.name in protegidos:
                continue                   # rotulado: nunca
            try:
                f.unlink()
                apagados += 1
            except OSError as e:
                log.debug("Evicção: nao consegui apagar %s (%s)", f.name, e)

        if not apagados:
            if not self._avisou_teto:
                log.warning("Captura PARADA: teto de %d atingido e nada pode ser evictado "
                            "(%d arquivo(s), todos rotulados ou em uso).",
                            self.max_arquivos, len(meus))
                self._avisou_teto = True
            return False

        self._inventario = (len(meus) - apagados, agora)
        # INFO e nao DEBUG: apagar arquivo e efeito colateral visivel, e quem investiga
        # "onde foi a captura de terca" precisa achar isto no log.
        log.info("Evicção da captura: %d arquivo(s) mais antigo(s) apagado(s) para manter o "
                 "teto de %d (a coleta continua)", apagados, self.max_arquivos)
        self._avisou_teto = False
        return True

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
            # Câmera com pipeline contínuo já é amostrada DE DENTRO do laço dele
            # (`Pipeline._processar_frame` chama `captura_dataset.amostrar`), e ele mantém
            # a conexão RTSP aberta o tempo todo. Abrir aqui seria uma SEGUNDA conexão
            # concorrente para a mesma câmera física — que a Intelbras não aceita — além
            # de coletar duas vezes a mesma coisa.
            #
            # O `lock_camera` abaixo NÃO cobre este caso: `Pipeline.iniciar()` só o segura
            # durante `camera.abrir()` e o solta em seguida, seguindo com a conexão viva.
            # O lock serializa aberturas, não posse da câmera.
            # Exige THREAD VIVA, não só instância registrada: `iniciar_camera` mantém a
            # instância no registro de propósito quando `iniciar()` levanta (para o
            # supervisor tentar de novo), e nesse estado `_processar_frame` nunca roda —
            # ninguém está amostrando nem detendo a conexão. Pular por "existe instância"
            # zeraria a coleta justamente na câmera com problema, em silêncio.
            from app.visao import pipeline as pipeline_mod
            pinst = pipeline_mod._instancias.get(cam_id)
            thread = getattr(pinst, "_thread", None) if pinst is not None else None
            if (pinst is not None and getattr(pinst, "deteccao_automatica", False)
                    and thread is not None and thread.is_alive()):
                log.debug("Camera %d: coleta pulada — pipeline contínuo já amostra "
                          "de dentro do laço e detém a conexão", cam_id)
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
