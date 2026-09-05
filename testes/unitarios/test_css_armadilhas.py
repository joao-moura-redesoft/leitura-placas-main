"""Duas armadilhas de CSS que já reincidiram neste projeto, prendidas por teste.

Não testam aparência (isso precisa de olho ou de navegador) — testam a REGRA de cascata,
que é determinística e onde os dois defeitos moravam:

1. **Hover de `<button>` com especificidade insuficiente.** `button:hover:not(:disabled)`
   (base.css) vale (0,2,1). Um override de uma classe só, `.minha-aba:hover`, vale (0,2,0)
   e PERDE — o botão fica com o verde primário por cima do fundo do componente e o texto
   costuma virar ilegível nele. Já aconteceu 4 vezes: `.cfg-tab` (1,52:1), `.feira-aba`
   (1,81:1), `.doc-tab` e `.nav-menu-trigger` (1,38:1, em todas as 23 telas).

2. **`[hidden]` perdendo de `display` do autor.** O `[hidden] { display: none }` vem da
   folha do NAVEGADOR, e a folha do autor sempre vence a do user-agent — então
   `button, .btn { display: inline-flex }` deixava todo `<button hidden>` visível. Sintoma
   real: o botão "Remover" do posto de demonstração aparecia antes de haver posto.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_WEB = Path(__file__).resolve().parents[2] / "app" / "web"
CSS = _WEB / "static" / "css"
TEMPLATES = _WEB / "templates"

# Comentários fora: eles CITAM os seletores errados de propósito (a documentação da
# armadilha), e sem tirá-los o teste acusaria a própria explicação.
_COMENTARIO = re.compile(r"/\*.*?\*/", re.S)


def _sem_comentarios(caminho: Path) -> str:
    return _COMENTARIO.sub("", caminho.read_text(encoding="utf-8"))


def _folhas():
    return sorted(CSS.glob("*.css"))


class TestHoverDeBotaoTemQueEmpatar:
    """Todo override de `:hover` que atinge um <button> precisa de `:not(:disabled)`."""

    # Seletores de UMA classe + :hover que estilizam elementos que SÃO <button> no HTML.
    # Lista explícita em vez de heurística: só o HTML diz se `.algo` é button ou div, e
    # adivinhar pelo nome daria falso positivo em cada `.card:hover` do sistema.
    BOTOES = ("nav-menu-trigger", "cfg-tab", "feira-aba", "doc-tab")

    @pytest.mark.parametrize("classe", BOTOES)
    def test_hover_tem_not_disabled(self, classe):
        for folha in _folhas():
            texto = _sem_comentarios(folha)
            for m in re.finditer(rf"\.{re.escape(classe)}((?::[\w-]+|\[[^\]]+\])*)\s*(?=[,{{])",
                                 texto):
                sufixo = m.group(1)
                if ":hover" not in sufixo and 'aria-expanded="true"' not in sufixo:
                    continue          # seletor de estado base, não disputa com o hover
                assert ":not(:disabled)" in sufixo, (
                    f"{folha.name}: `.{classe}{sufixo}` vale (0,2,0) e perde de "
                    f"`button:hover:not(:disabled)` (0,2,1) — o fundo do componente "
                    f"não é aplicado e o texto fica ilegível. Escreva "
                    f"`.{classe}{sufixo}:not(:disabled)`.")

    def test_a_regra_generica_continua_sendo_a_de_referencia(self):
        """Se `button:hover` deixar de ter `:not(:disabled)`, o teste acima perde o sentido.

        Prende a premissa: é dela que vem o (0,2,1) que os overrides têm de empatar.
        """
        base = _sem_comentarios(CSS / "base.css")
        assert "button:hover:not(:disabled)" in base, (
            "a regra de referência mudou — revise a premissa de TestHoverDeBotaoTemQueEmpatar")


class TestHiddenFunciona:

    def test_existe_reset_global_de_hidden(self):
        """Sem isto, `elemento.hidden = true` não esconde <button> nenhum."""
        alcance = _sem_comentarios(CSS / "tokens.css")
        assert re.search(r"\[hidden\]\s*\{[^}]*display\s*:\s*none\s*!important", alcance), (
            "falta `[hidden] { display: none !important; }` em tokens.css — a folha do "
            "autor vence a do navegador, então `button, .btn { display: inline-flex }` "
            "deixa todo <button hidden> VISÍVEL")

    def test_o_reset_esta_na_folha_que_todas_as_bases_carregam(self):
        """`tokens.css` e não `base.css`: as telas de login usam `auth.css`.

        Se o reset migrar para base.css, `/login` e `/criar-admin` voltam a ter o bug.
        """
        for base in ("base.html", "auth_base.html"):
            html = (TEMPLATES / base).read_text(encoding="utf-8")
            assert "tokens.css" in html, f"{base} não carrega tokens.css"

    def test_nenhuma_regra_revela_elemento_hidden(self):
        """O `!important` global quebraria quem mostrasse um `[hidden]` de propósito.

        `:not([hidden])` é o padrão correto e não conta — ele só age quando NÃO está
        escondido. O que este teste barra é `[hidden] { display: block }` e parentes.
        """
        for folha in _folhas():
            texto = _sem_comentarios(folha)
            for m in re.finditer(r"([^{}]*\[hidden\][^{}]*)\{([^}]*)\}", texto):
                seletor, corpo = m.group(1), m.group(2)
                if ":not([hidden])" in seletor:
                    continue
                disp = re.search(r"display\s*:\s*([\w-]+)", corpo)
                if disp:
                    assert disp.group(1) == "none", (
                        f"{folha.name}: `{seletor.strip()}` dá "
                        f"`display: {disp.group(1)}` a um elemento [hidden] — o reset "
                        f"global com !important vai anular isso")
