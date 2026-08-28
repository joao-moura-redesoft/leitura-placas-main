"""Double-checked locking genérico — compartilhado pelas 4 fábricas de singleton cacheado
(`obter_detector_leitura`/`obter_detector_rapido` em detector.py,
`obter_ocr_leitura`/`obter_ocr_rapido` em ocr/auto.py).

As 4 repetiam a MESMA lógica de controle (checa fora do lock, checa de novo dentro,
reconstrói, atribui) — só COMO constroem o objeto muda entre elas. Extraído aqui só o
controle; a construção continua em cada função, via a closure `construir`.
(Review de 28/08/2026, achado B3.)

RESTRIÇÃO que motivou este desenho, e não uma classe/cache unificado: os testes
(`conftest.py::_sem_visao`, `test_fabrica_detector_leitura.py`) monkeypatcham as 4 funções
E as globals de cada uma (`_detector_leitura`, `_detector_leitura_id`, etc.) pelo NOME
exato, como atributos de módulo top-level, e travam que os slots de leitura/rápido são
independentes. Esta função não guarda estado nenhum — cada chamadora continua dona das
próprias globals e do próprio lock.

CORREÇÃO (achado do review de 28/08/2026): a primeira versão devolvia o valor construído
e deixava a atribuição às globals para DEPOIS do `return` — ou seja, depois que o `with
lock` já tinha liberado o lock. Duas threads carregando o mesmo detector ao mesmo tempo
podiam as DUAS ver a global ainda vazia (nenhuma tinha gravado ainda) e construir cada
uma a sua própria instância — exatamente a corrida que o lock existe para impedir. Por
isso `definir` agora é um parâmetro: quem chama grava a global de dentro do próprio
`resolver`, ainda com o lock seguro.
"""
from __future__ import annotations
import threading
from typing import Callable, Tuple


def resolver(obter_atual: Callable[[], Tuple[object, object]], ident, lock: threading.Lock,
            construir: Callable[[], object], definir: Callable[[object, object], None]):
    """Devolve o valor cacheado (construindo-o primeiro, se preciso).

    `obter_atual` DEVE ler as globals AO VIVO a cada chamada (não capturar um valor antes
    de invocar `resolver`) — é o que faz o recheck dentro do lock valer alguma coisa: outra
    thread pode ter terminado de construir enquanto esta esperava o lock.

    `definir(valor, ident)` é chamado AINDA DENTRO DO LOCK, imediatamente após construir —
    é o que fecha a corrida: nenhuma outra thread pode ver a global vazia entre "terminei
    de construir" e "gravei o resultado".
    """
    valor, ident_atual = obter_atual()
    if valor is not None and ident_atual == ident:
        return valor
    with lock:
        valor, ident_atual = obter_atual()   # reconfirma DENTRO do lock
        if valor is not None and ident_atual == ident:
            return valor
        valor = construir()
        definir(valor, ident)
        return valor
