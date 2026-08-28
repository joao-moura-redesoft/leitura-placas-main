"""Rótulo de origem (câmera / veículo) carregado junto com a thread que roda o OCR.

O log de OCR nasce no fundo da pilha: `AutoOCR` e `OCR` não sabem — e não deveriam
saber — de qual câmera veio o recorte nem a qual veículo rastreado ele pertence. Sem
esse dado, porém, as linhas de dois pipelines se intercalam no mesmo arquivo e não há
como atribuir nenhuma delas. Pior: quando nenhum engine valida, não vem linha de
tracker atrás, e o bloco inteiro fica órfão. Foi exatamente o que aconteceu no log de
13/08/2026, com os IDs 360-374 de uma câmera intercalados com 312-314 de outra.

Passar câmera e track por parâmetro atravessaria seis assinaturas para servir só ao
log. Aqui o rótulo mora em `threading.local` — cada pipeline já roda na sua própria
thread — e `instalar()` o costura na frente da mensagem, sem que os pontos de log
precisem saber que ele existe.

    with contexto_log.usar(camera=3, track=365):
        ocr.ler(crop)      # → "[cam3 trk365] OCR crop=17x6px ..."

CUIDADO: thread nova nasce SEM rótulo (`threading.local` não é herdado). Quem cria uma
thread para chamar OCR precisa reabrir o contexto lá dentro com `herdar()`.

Quem usa `herdar()` hoje é `leitura._em_paralelo`, que abre uma thread por câmera do bico.
O `AutoOCRPaddle` também usava, para rodar AutoOCR e PaddleOCR em paralelo, até 25/08/2026:
o paralelismo saiu junto com a arbitragem entre engines, porque no pool plano os três
modelos do fast-plate-ocr custam 62 ms contra 747 ms do Paddle — paralelizar economizaria
62 ms em troca de uma thread.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager

_local = threading.local()

# Estado = (camera, track), e NÃO a string já formatada: com a string, aninhar
# `usar(track=...)` dentro de `usar(camera=...)` obrigava a extrair a câmera de volta do
# texto ("[cam3] " → "3") para não perdê-la. Guardar os componentes torna a herança uma
# atribuição, e a formatação passa a ter um caminho só.
_VAZIO: tuple = (None, None)


def _estado() -> tuple:
    return getattr(_local, "estado", _VAZIO)


def rotulo() -> str:
    """`"[cam3 trk365] "` — ou string vazia fora de qualquer contexto."""
    camera, track = _estado()
    partes = []
    if camera is not None:
        partes.append("cam%s" % camera)
    if track is not None:
        partes.append("trk%s" % track)
    if not partes:
        return ""
    return "[%s] " % " ".join(partes)


@contextmanager
def usar(camera=None, track=None):
    """Rotula tudo que for logado nesta thread enquanto o bloco durar.

    Aninhar preserva a câmera do contexto externo: o pipeline a declara uma vez no seu
    laço e o laço de tracks só acrescenta o `track`.
    """
    anterior = _estado()
    if camera is None:
        camera = anterior[0]
    _local.estado = (camera, track)
    try:
        yield
    finally:
        _local.estado = anterior


def capturar() -> tuple:
    """Contexto atual, para repassar a uma thread que será criada (ver `herdar`)."""
    return _estado()


@contextmanager
def herdar(contexto: tuple):
    """Reabre, numa thread nova, o contexto capturado na thread que a criou.

    `threading.local` não atravessa `Thread(...)`: sem isto o trabalho de uma thread de OCR
    logaria sem dono, intercalado com o das câmeras.

    Quem usa hoje é `leitura._em_paralelo`, que abre uma thread por câmera do bico. O
    `AutoOCRPaddle` também usava, para rodar AutoOCR e PaddleOCR em paralelo, até
    25/08/2026 — ver o docstring do módulo.
    """
    anterior = _estado()
    _local.estado = contexto
    try:
        yield
    finally:
        _local.estado = anterior


_instalado = False


def instalar() -> None:
    """Costura o rótulo em todo record dos loggers `app.*` (idempotente).

    Via `setLogRecordFactory`, e não via `logging.Filter`: filtro instalado num logger só
    vale para o que é logado NELE — um filtro em `logging.getLogger("app.visao")` não vê
    nada vindo de `app.visao.ocr.auto`, que é justamente onde o log de OCR nasce. Filtro
    em HANDLER funcionaria, mas exigiria alcançar todo handler já instalado e todo que
    venha depois. A fábrica de records é o único ponto por onde todos passam.
    """
    global _instalado
    if _instalado:
        return
    anterior = logging.getLogRecordFactory()

    def fabrica(name, level, fn, lno, msg, *args, **kwargs):
        record = anterior(name, level, fn, lno, msg, *args, **kwargs)
        marca = rotulo()
        # Só o código do projeto: prefixar mensagem de biblioteca de terceiro só porque
        # ela caiu dentro do bloco confunde mais do que ajuda.
        if marca and name.startswith("app."):
            record.msg = marca + str(record.msg)
        return record

    logging.setLogRecordFactory(fabrica)
    _instalado = True


# Depois de quantas falhas SEGUIDAS um componente deixa de estar "tropeçando" e passa a
# estar inoperante. 10 é ~2 s de vídeo a 5 fps de detecção: curto para aparecer rápido,
# longo para não gritar por causa de um quadro corrompido isolado.
FALHAS_PARA_ALARMAR = 10


class ContadorDeFalhas:
    """Transforma uma enxurrada de WARNINGs iguais em UM alarme, e avisa quando recupera.

    Mora aqui, junto do rótulo de log, porque o problema é de LOG e não do componente que
    falha: `detector.py` e `ambiente.py` precisam dos dois, e `contexto_log` é o módulo que
    os dois já importam sem criar ciclo.

    O log de 24/08/2026 tem 849 linhas de `VehicleDetector: falha na inferência` e 846 de
    `AjustadorAmbiente: falha ao processar frame`, cada uma um WARNING solto. Ninguém lê 849
    WARNINGs — e o que aconteceu de fato foi que, por 3,5 minutos, o detector de veículo
    estava MORTO e `deteccoes.tipo_veiculo` chegou nulo ao banco, indistinguível no
    histórico de "não havia veículo no quadro".

    A falha individual continua logada, mas em DEBUG. Ao cruzar o limiar sai UMA linha de
    ERROR. A recuperação também é logada: sem ela, quem vê o ERROR não sabe se o problema
    durou 2 segundos ou 2 horas.
    """

    __slots__ = ("nome", "limiar", "_seguidas", "_alarmado")

    def __init__(self, nome: str, limiar: int = FALHAS_PARA_ALARMAR) -> None:
        self.nome = nome
        self.limiar = max(1, limiar)
        self._seguidas = 0
        self._alarmado = False

    def falhou(self, erro: object) -> None:
        self._seguidas += 1
        if self._seguidas >= self.limiar and not self._alarmado:
            self._alarmado = True
            _log_alarme.error(
                "%s INOPERANTE: %d falhas seguidas (última: %s). Enquanto durar, o "
                "resultado deste componente sai VAZIO e não há como distinguir isso de "
                "'nada detectado'.", self.nome, self._seguidas, erro)
        else:
            _log_alarme.debug("%s: falha (%s) [%d seguida(s)]",
                              self.nome, erro, self._seguidas)

    def funcionou(self) -> None:
        if self._alarmado:
            _log_alarme.warning("%s VOLTOU depois de %d falhas seguidas",
                                self.nome, self._seguidas)
        self._seguidas = 0
        self._alarmado = False


_log_alarme = logging.getLogger(__name__)
