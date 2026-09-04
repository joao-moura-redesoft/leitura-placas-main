"""O JavaScript embutido nos templates tem que PARSEAR.

Existe por causa de um defeito de 03/09/2026 que passou por toda a suíte e chegou na
máquina da feira: uma string de aspas simples com quebra de linha no meio, dentro de um
`<script>` de `posto.html`.

Por que nada pegou:

  - os 1049 testes de backend passaram — nenhum deles carrega a página;
  - `jinja2.get_template()` passou — ele valida o JINJA, não o JavaScript;
  - o servidor subiu e respondeu 200 — o HTML é servido igual.

E o sintoma não ajudava: um SyntaxError num `<script>` derruba o BLOCO INTEIRO. Nenhuma
função é definida, `carregar()` nunca roda, e a tela aparece com os placeholders — sem
erro nenhum visível, parecendo "o posto ficou vazio". Só o console do navegador contava.

Este teste é a rede que faltava. Roda o `node --check` sobre cada bloco de script,
trocando as expressões Jinja por literais neutros antes (o que se valida é o código que
o navegador recebe, não o gabarito).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[2] / "app" / "web" / "templates"

# `<script src=...>` não tem corpo próprio para validar — o arquivo dele é servido
# direto de /static e parseado pelo navegador por conta.
_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)
_EXPR = re.compile(r"\{\{.*?\}\}", re.S)
_TAG = re.compile(r"\{%.*?%\}", re.S)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    # `skipif` e não falha: a suíte tem de rodar numa máquina sem Node (o projeto é
    # Python puro). Onde houver Node — dev e CI — a rede está armada.
    reason="node não instalado — sem ele não há como parsear o JS",
)


def _blocos(html: str) -> list[str]:
    return [m.group(1) for m in _SCRIPT.finditer(html)]


def _sem_jinja(js: str) -> str:
    """`{{ x }}` vira `0` e `{% ... %}` some.

    Sem isto, `const ID = {{ empresa_id }};` seria erro de sintaxe e o teste acusaria
    todo template que usa Jinja dentro de script — ou seja, quase todos.
    """
    return _TAG.sub("", _EXPR.sub("0", js))


@pytest.mark.parametrize(
    "template", sorted(p.name for p in TEMPLATES.glob("*.html")))
def test_javascript_parseia(template):
    arquivo = TEMPLATES / template
    for i, bloco in enumerate(_blocos(arquivo.read_text(encoding="utf-8")), 1):
        codigo = _sem_jinja(bloco)
        if not codigo.strip():
            continue
        r = subprocess.run(
            ["node", "--input-type=module", "--check"],
            input=codigo, text=True, encoding="utf-8", capture_output=True)
        assert r.returncode == 0, (
            f"{template}, bloco <script> nº {i}: o JavaScript não parseia.\n"
            f"Um SyntaxError aqui derruba o script INTEIRO da página — a tela abre com os "
            f"placeholders e sem erro visível.\n\n{r.stderr[:800]}")
