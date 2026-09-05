"""Escrita atômica de arquivo — o leitor vê o conteúdo antigo ou o novo, nunca metade.

`write_text`/`imwrite` truncam o arquivo e só então escrevem: entre as duas coisas o
arquivo EXISTE e está incompleto. Quem lê nesse instante pega lixo, e como o arquivo fica
íntegro logo depois o diagnóstico é péssimo — o sintoma some antes de alguém ir olhar.

Nenhum lock resolve isso, porque quem lê está em outro módulo/processo e não conhece o
lock de quem escreve: `config.carregar()` é chamado pelo middleware de autenticação a cada
request com api_key, e `/api/bicos/{id}/preview.jpg` serve o JPEG com `FileResponse`
enquanto uma leitura o reescreve. Medido nesta base (auditoria 05/09/2026):

    config.txt  — 185 de 1155 leituras de auth pegaram o arquivo truncado, e cada uma
                  recusa com 401 uma chamada VÁLIDA de /api/leitura de um posto.
    preview.jpg — 17.400 leituras truncadas contra 253 íntegras.

A correção é sempre a mesma: escreve num temporário AO LADO do destino e `os.replace`.
`os.replace` é atômico no mesmo volume, em POSIX e no Windows (`MoveFileEx` com
`REPLACE_EXISTING`). Ao lado do destino de propósito: `tempfile.gettempdir()` pode estar em
outro volume, e aí `replace` vira cópia e deixa de ser atômico.

O temporário leva PID + contador no nome. Um nome fixo (`destino.tmp`) reintroduz a corrida
que a função existe para tirar: dois escritores do MESMO arquivo escrevem por cima do mesmo
temporário e o `replace` do primeiro publica um arquivo que o segundo ainda está montando.
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

# Sufixo único por escritor. `itertools.count` é atômico o bastante aqui (o `next` de um
# count roda inteiro sob o GIL) e o PID separa processos distintos apontando para o mesmo
# volume — o caso do container que compartilha ./dados com o host.
_seq = itertools.count()


def _tmp_ao_lado(destino: Path) -> Path:
    return destino.with_suffix(f"{destino.suffix}.{os.getpid()}-{next(_seq)}.tmp")


# Tentativas de `os.replace` e espera entre elas.
#
# NECESSÁRIO NO WINDOWS, e é a parte contra-intuitiva desta correção: `os.replace` sobre um
# destino que OUTRO processo/thread tem aberto para leitura falha com
# `PermissionError [WinError 5]`. O share mode padrão do CRT não permite renomear por cima
# de um handle aberto — em POSIX o rename simplesmente funciona (o leitor segue com o
# inode antigo). Medido aqui: com um leitor em laço, 232 de 300 replaces falharam.
#
# Sem o retry, esta correção trocaria "leitor vê arquivo truncado" por "gravação de config
# falha com 500" — pior, porque perde a escrita do admin em vez de só atrasá-la. O leitor
# fica com o arquivo aberto por microssegundos (um `read_text`/`FileResponse`), então uma
# espera curta basta: o pior caso medido some com 5 tentativas.
_REPLACE_TENTATIVAS = 10
_REPLACE_ESPERA_SEG = 0.02


def _com_retry_de_permissao(acao):
    """Roda `acao()` repetindo enquanto o Windows recusar por handle aberto.

    Serve aos DOIS lados da troca — o `os.replace` do escritor e o `open` do leitor —,
    porque a causa é a mesma: enquanto o `MoveFileEx` republica o arquivo, o outro lado vê
    PermissionError. Backoff crescente: um leitor lento (JPEG de preview) segura o handle
    por mais tempo que um `read_text` de config.
    """
    for tentativa in range(_REPLACE_TENTATIVAS):
        try:
            return acao()
        except PermissionError:
            if tentativa == _REPLACE_TENTATIVAS - 1:
                raise
            time.sleep(_REPLACE_ESPERA_SEG * (tentativa + 1))
    raise AssertionError("inalcançável")


def _replace_com_retry(tmp: Path, destino: Path) -> None:
    """`os.replace` tolerante ao leitor concorrente do Windows (ver constantes acima)."""
    _com_retry_de_permissao(lambda: os.replace(tmp, destino))

def escrever_bytes_atomico(destino: Path, dados: bytes) -> None:
    """Publica `dados` em `destino` de uma vez só."""
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_ao_lado(destino)
    try:
        tmp.write_bytes(dados)
        _replace_com_retry(tmp, destino)
    except BaseException:
        # Sem isto, uma falha no meio (disco cheio, permissão) deixa o temporário para trás
        # a cada tentativa — e ninguém nunca os apaga, porque o nome muda toda vez.
        tmp.unlink(missing_ok=True)
        raise


def escrever_texto_atomico(destino: Path, texto: str, encoding: str = "utf-8") -> None:
    escrever_bytes_atomico(Path(destino), texto.encode(encoding))


def ler_bytes_com_retry(origem: Path) -> bytes:
    """Lê o arquivo tolerando o `os.replace` de um escritor concorrente.

    O ESPELHO do problema descrito em `_replace_com_retry`, e igualmente específico do
    Windows: durante a janela em que o `MoveFileEx` do escritor troca o arquivo, um `open`
    do leitor falha com `PermissionError [Errno 13]`. Medido nesta base: 27 leituras de
    `config.txt` em ~4.000 falharam assim durante 400 gravações.

    Isso NÃO é regressão da escrita atômica — antes dela o leitor não levantava, ele lia
    lixo em silêncio (que é o bug pior). Mas `config.carregar()` roda no middleware de
    autenticação, e uma exceção ali derruba a request de um posto do mesmo jeito que o 401
    de antes. A escrita já é atômica, então o conteúdo nunca está pela metade: basta
    esperar a troca terminar e ler de novo.
    """
    return _com_retry_de_permissao(Path(origem).read_bytes)


def ler_texto_com_retry(origem: Path, encoding: str = "utf-8") -> str:
    """`ler_bytes_com_retry` decodificado. Ver o docstring dele."""
    return ler_bytes_com_retry(origem).decode(encoding)


def escrever_json_atomico(destino: Path, dados, *, indent: int = 2) -> None:
    escrever_texto_atomico(Path(destino),
                           json.dumps(dados, indent=indent, ensure_ascii=False))


def imwrite_atomico(destino: Path, imagem, qualidade: int = 80,
                    *, tolerar_falha: bool = False) -> bool:
    """`cv2.imwrite` que não deixa o JPEG pela metade. False = não gravou.

    O preview de bico é o caso que motivou isto: nome FIXO (`preview_bico_{id}.jpg`)
    reescrito a cada leitura, e `/api/bicos/{id}/preview.jpg` serve o mesmo arquivo com
    `FileResponse` enquanto outro operador está com a tela aberta. Medido em 05/09/2026:
    17.400 leituras pegaram JPEG sem marcador de fim contra 253 íntegras — dois PCs na
    tela de captura do mesmo bico é o caso comum, não o raro.

    Encoda para memória e publica com `os.replace`. O encode é o mesmo que o `imwrite`
    fazia; o que muda é que o arquivo de destino só existe pronto.

    `tolerar_falha=True` loga e devolve False em vez de propagar erro de I/O. É o certo para
    imagem de DIAGNÓSTICO (preview de bico): ela não pode derrubar a leitura de placa que
    o roteador do posto está esperando — o pior caso aceitável é a tela mostrar o quadro
    anterior. Para snapshot de histórico o default (propagar) é o certo: ali o arquivo é
    referenciado por uma linha do banco, e falhar em silêncio deixaria a linha apontando
    para arquivo que não existe.

    `cv2` é importado aqui dentro, não no topo: este módulo é usado por `app/core/config`,
    que roda no boot antes de qualquer coisa de visão e não deve arrastar o OpenCV (nem os
    conflitos de OpenMP que `app/core/nativo.py` descreve) só para gravar um texto.
    """
    import cv2
    destino = Path(destino)
    ok, buf = cv2.imencode(destino.suffix or ".jpg", imagem,
                           [int(cv2.IMWRITE_JPEG_QUALITY), qualidade])
    if not ok:
        return False
    try:
        escrever_bytes_atomico(destino, buf.tobytes())
    except OSError as e:
        if not tolerar_falha:
            raise
        log.warning("Não gravei %s (%s) — mantido o arquivo anterior", destino, e)
        return False
    return True
