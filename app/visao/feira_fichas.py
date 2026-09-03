"""Modo feira: fichas de demonstração por placa (só exibição no kiosk).

Para que serve
--------------
No estande, quando o carrinho de demonstração é reconhecido (ver `app/visao/feira.py`),
a tela de vitrine (`/feira`) mostra um card "Bem-vindo!" com dados do veículo: tipo de
combustível, modelo, cor, ano. Esses dados NÃO existem offline — o combustível de verdade
vem da apiplacas, que precisa de internet e não estará disponível na feira. Este módulo
guarda uma ficha local por placa, editável na tela secreta de configuração, para o card
ter o que exibir.

O que este módulo deliberadamente NÃO é
---------------------------------------
Não é dado de produção. A ficha é puramente cosmética, lida só pelo kiosk. Nada aqui toca
no pipeline, na eleição/fusão, na tabela `veiculos` (cache da apiplacas) nem na taxa de
acerto. É o mesmo cuidado do cabeçalho de `feira.py`: dado sintético de demonstração fica
isolado do que se mede. Por isso vive num arquivo próprio, ao lado do `config.txt`, e não
mistura com o cache de veículos reais.
"""
from __future__ import annotations

import json
import logging

from app.core import config
from app.visao import feira

log = logging.getLogger(__name__)

NOME_ARQUIVO = "feira_fichas.json"


def _arquivo():
    """Caminho do JSON de fichas — ao lado do config.txt (mesmo diretório).

    Calculado a cada chamada, não fixado no import: assim acompanha `config.CONFIG_PATH`
    (fixo no boot em produção; trocado por tmp_path na suíte de testes). `CONFIG_PATH` pode
    ser relativo ("config.txt"); `.parent` vira "." e o arquivo nasce junto do config, que
    é onde o operador procura.
    """
    return config.CONFIG_PATH.parent / NOME_ARQUIVO

# Campos de uma ficha. Ordem = ordem de exibição no editor. Tudo string livre: o card só
# mostra o texto, e combustível é rótulo ("Flex", "Gasolina", "Elétrico") cujo ícone o
# frontend escolhe. `ano` fica string por consistência (config inteiro é do backend real).
CAMPOS = ("apelido", "modelo", "combustivel", "cor", "ano", "mensagem")

# Ficha embutida do carrinho canônico (ver memória carro-demo-feira-placa). Existe para a
# tela NUNCA aparecer vazia numa instalação recém-criada: sem isto, reconhecer MOK-3H92
# antes de alguém preencher a ficha mostraria um card sem dado nenhum bem na hora da demo.
PADROES: dict[str, dict[str, str]] = {
    "MOK3H92": {
        "apelido": "Carro de demonstração",
        "modelo": "Volkswagen Nivus",
        "combustivel": "Flex",
        "cor": "Cinza",
        "ano": "2024",
        "mensagem": "Bem-vindo! Leitura reconhecida com sucesso.",
    },
}


def ficha_vazia() -> dict[str, str]:
    """Uma ficha com todos os campos em branco — base para o editor montar uma linha nova."""
    return {c: "" for c in CAMPOS}


def _limpar(bruta: dict) -> dict[str, str]:
    """Mantém só os `CAMPOS` conhecidos, tudo coagido a string enxuta.

    Descarta chave estranha (o arquivo é editável à mão) e evita que um número/None vindo
    do JSON quebre o template, que espera texto.
    """
    return {c: str(bruta.get(c, "") or "").strip() for c in CAMPOS}


def carregar_fichas() -> dict[str, dict[str, str]]:
    """Todas as fichas, chaveadas pela placa normalizada, com os `PADROES` como base.

    Os padrões entram por baixo e o arquivo por cima: assim o carrinho canônico sempre tem
    ficha, mas o operador pode sobrescrevê-la. Falha de leitura (arquivo corrompido, disco)
    NÃO derruba a demo — cai nos padrões e loga, mesmo espírito do resto do modo feira.
    """
    fichas = {feira.normalizar(k): _limpar(v) for k, v in PADROES.items()}
    caminho = _arquivo()
    if caminho.exists():
        try:
            dados = json.loads(caminho.read_text(encoding="utf-8"))
            if isinstance(dados, dict):
                for placa, ficha in dados.items():
                    norm = feira.normalizar(placa)
                    if norm and isinstance(ficha, dict):
                        fichas[norm] = _limpar(ficha)
        except Exception as e:
            log.warning("Falha ao ler %s — usando fichas padrão (%s)", caminho, e)
    return fichas


def salvar_fichas(fichas: dict) -> dict[str, dict[str, str]]:
    """Grava as fichas normalizadas e devolve o que ficou gravado.

    Placa vazia é descartada. Grava o conjunto inteiro (não faz merge): o editor manda a
    lista completa, e um merge silencioso deixaria ficha removida ressuscitar.
    """
    limpo = {
        feira.normalizar(placa): _limpar(ficha)
        for placa, ficha in (fichas or {}).items()
        if feira.normalizar(placa) and isinstance(ficha, dict)
    }
    _arquivo().write_text(json.dumps(limpo, ensure_ascii=False, indent=2), encoding="utf-8")
    return limpo


def ficha_de(placa: str | None) -> dict[str, str] | None:
    """A ficha da placa (normalizada), ou None se não houver uma cadastrada.

    None significa "sem card de dados" — o kiosk ainda pode saudar pela placa, mas não
    inventa combustível/modelo.
    """
    norm = feira.normalizar(placa)
    if not norm:
        return None
    return carregar_fichas().get(norm)
