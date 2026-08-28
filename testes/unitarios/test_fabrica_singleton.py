"""app/visao/_fabrica_singleton.resolver — double-checked locking genérico.

Achado do review de 28/08/2026: a primeira versão devolvia o valor construído e deixava
a atribuição às globals para DEPOIS do `return` — ou seja, depois que o `with lock` já
tinha liberado o lock. Duas threads carregando o mesmo detector ao mesmo tempo podiam as
DUAS ver a global ainda vazia e construir cada uma a sua própria instância. Corrigido
passando um `definir` que grava a global AINDA DENTRO do lock, antes de `resolver` voltar.
"""
from __future__ import annotations

import threading
import time

from app.visao._fabrica_singleton import resolver


def test_construir_uma_unica_vez_sob_concorrencia():
    lock = threading.Lock()
    estado = {"valor": None, "id": None}
    construcoes = []

    def obter_atual():
        return estado["valor"], estado["id"]

    def definir(v, i):
        estado["valor"], estado["id"] = v, i

    def construir():
        # Simula carga de modelo: devagar o bastante para as duas threads chegarem
        # aqui ANTES de qualquer uma gravar o resultado, se a corrida existir.
        time.sleep(0.05)
        construcoes.append(1)
        return object()

    resultados = []

    def chamar():
        resultados.append(resolver(obter_atual, "ident-1", lock, construir, definir))

    t1 = threading.Thread(target=chamar)
    t2 = threading.Thread(target=chamar)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(construcoes) == 1, "construiu mais de uma vez sob concorrência — corrida"
    assert resultados[0] is resultados[1], "as duas chamadas devem devolver A MESMA instância"
    assert estado["valor"] is resultados[0]
    assert estado["id"] == "ident-1"


def test_cache_hit_nao_constroi_de_novo():
    lock = threading.Lock()
    estado = {"valor": object(), "id": "ident-1"}
    construcoes = []

    valor = resolver(lambda: (estado["valor"], estado["id"]), "ident-1", lock,
                     lambda: construcoes.append(1) or object(),
                     lambda v, i: None)

    assert valor is estado["valor"]
    assert construcoes == []


def test_identidade_diferente_reconstroi():
    lock = threading.Lock()
    estado = {"valor": "antigo", "id": "ident-1"}

    def definir(v, i):
        estado["valor"], estado["id"] = v, i

    valor = resolver(lambda: (estado["valor"], estado["id"]), "ident-2", lock,
                     lambda: "novo", definir)

    assert valor == "novo"
    assert estado == {"valor": "novo", "id": "ident-2"}
