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
thread para chamar OCR precisa reabrir o contexto lá dentro com `herdar()` — ver
`AutoOCRPaddle.ler_detalhado`, que roda os dois engines em paralelo.
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

    `threading.local` não atravessa `Thread(...)`: sem isto o trabalho paralelo do
    `AutoOCRPaddle` logaria sem dono, intercalado com o das câmeras.
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
