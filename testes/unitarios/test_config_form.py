"""Consistência entre a tela de configuração e as chaves aceitas pela API.

`POST /api/config` rejeita com 400 qualquer chave fora de `config.PADROES`
(app/web/api.py `CHAVES_CONFIG`). O formulário de `configuracao.html` faz
`new FormData(form)` e manda TODOS os campos de uma vez — se um `name="..."` do HTML
não existir em `PADROES`, o salvamento da tela INTEIRA quebra com 400, não só aquele
campo. É a mesma classe de erro que `processar_a_cada_n_frames` quase causou ao ser
removido pela metade.
"""
from __future__ import annotations
import re
from pathlib import Path

from app.core import config

_HTML = Path("app/web/templates/configuracao.html").read_text(encoding="utf-8")
_NOMES_NO_FORMULARIO = set(re.findall(r'name="([a-z_]+)"', _HTML))


def test_todo_campo_do_formulario_e_uma_chave_conhecida():
    desconhecidos = _NOMES_NO_FORMULARIO - set(config.PADROES.keys())
    assert not desconhecidos, (
        f"campo(s) {desconhecidos} no formulário não existem em config.PADROES — "
        "salvar a tela vai dar 400 'chaves desconhecidas'"
    )


def test_processar_a_cada_n_frames_foi_removido():
    """Regressão: existia um campo (e uma chave, e um `self.skip_n`) que nunca eram
    lidos em lugar nenhum do pipeline — 'processar a cada N frames' não fazia nada."""
    assert "processar_a_cada_n_frames" not in config.PADROES
    assert "processar_a_cada_n_frames" not in _NOMES_NO_FORMULARIO
