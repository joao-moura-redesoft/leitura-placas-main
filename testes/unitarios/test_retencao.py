"""Regressão: `_arquivo_de_url` só aceitava o prefixo "/static/" por texto, sem
resolver o caminho final nem confirmar que ele continua dentro de app/web/static/.
Hoje os valores gravados em `deteccoes`/`chamadas` vêm só de fontes internas
controladas, mas a purga não deveria confiar apenas no prefixo — um "/static/../x"
(ou qualquer sequência de travessia) precisa ser recusado, não resolvido para fora
do diretório base.
"""
from __future__ import annotations
from pathlib import Path

from app.operacao.retencao import _arquivo_de_url


def test_url_normal_resolve_dentro_de_static():
    caminho = _arquivo_de_url("/static/snapshots/x.jpg")
    base = Path("app/web/static").resolve()
    assert caminho == (base / "snapshots" / "x.jpg")
    assert caminho.is_relative_to(base)


def test_sem_prefixo_static_e_none():
    assert _arquivo_de_url("/outro/caminho/x.jpg") is None
    assert _arquivo_de_url("") is None
    assert _arquivo_de_url(None) is None


def test_travessia_de_diretorio_e_recusada(caplog):
    """O ponto central da correção: nem `.startswith("/static/")` nem a montagem do
    Path evitam sozinhos um "../" no meio do caminho — só resolver e conferir
    `relative_to(base)` garante que o resultado não escapou do diretório esperado."""
    with caplog.at_level("WARNING"):
        resultado = _arquivo_de_url("/static/../../etc/passwd")
    assert resultado is None
    assert any("fora de app/web/static" in r.message for r in caplog.records)


def test_travessia_disfarcada_em_subpasta_tambem_e_recusada():
    resultado = _arquivo_de_url("/static/snapshots/../../../secreto.txt")
    assert resultado is None
