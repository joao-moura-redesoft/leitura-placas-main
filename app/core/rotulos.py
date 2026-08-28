"""Quais arquivos de `app/web/static/snapshots/` nenhuma limpeza automática pode apagar.

Rótulo humano é a coisa mais cara de reproduzir neste projeto, e a pasta de snapshots é
gitignored — não existe cópia em lugar nenhum. Apagar uma captura já rotulada transforma
trabalho de gente numa linha do dataset apontando para arquivo inexistente, que é um modo
de falha que este projeto JÁ teve (commit 2252896, "Corrige o caminho das capturas no
dataset e tira arquivo faltando da acuracia").

Vive em `app/core/` porque DOIS subsistemas independentes precisam da mesma resposta:

  app/visao/captura_dataset.py   evicção por teto de arquivos da coleta
  app/operacao/retencao.py       purga por prazo e por teto de leituras do histórico

E só-stdlib de propósito: `retencao.py` não deve importar `cv2` (que é o que
`captura_dataset` arrasta) para decidir o que pode apagar.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_DATASET = Path("testes/dataset.json")


def protegidos() -> set[str] | None:
    """Nomes de arquivo que o dataset referencia — nenhuma limpeza pode tocá-los.

    Devolve os NOMES (sem diretório): a pasta de snapshots é plana e o nome já começa com
    timestamp em milissegundos, então é chave suficiente e não depende de o dataset e o
    chamador concordarem sobre o caminho relativo.

    Falha em ler devolve `None`, e quem chama tem de ABORTAR a limpeza inteira — nunca
    tratar como conjunto vazio, que é o mais perigoso possível. Não apagar nada é sempre
    recuperável; apagar rótulo humano não é.
    """
    try:
        dados = json.loads(_DATASET.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # Sem dataset não há rótulo a proteger — mas AVISA, porque "arquivo ausente" é
        # ambíguo: pode ser projeto novo (legítimo) ou processo rodando do diretório errado
        # (e aí a limpeza correria sem proteção nenhuma). O caminho é relativo de propósito,
        # a mesma convenção de `SNAPSHOT_DIR`, e o Dockerfile leva `testes/dataset.json`
        # (o .dockerignore exclui só `fotos/` e `resultados/`).
        log.warning("Limpeza sem protecao de rotulo: %s nao encontrado a partir de %s",
                    _DATASET, Path.cwd())
        return set()
    except (OSError, ValueError) as e:
        log.warning("Limpeza abortada: nao consegui ler o dataset (%s)", e)
        return None
    return {Path(f["arquivo"]).name for f in dados.get("fotos", []) if f.get("arquivo")}
