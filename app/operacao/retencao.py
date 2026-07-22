"""Retenção de dados: apaga `deteccoes`/`chamadas` antigas (e os JPEGs correspondentes).

Sem isso, um servidor multi-tenant rodando meses/anos acumula linha e imagem para
sempre — disco e tabela crescem sem limite, silenciosamente. Roda 1x por dia (retenção
é medida em dias, não faz sentido checar com mais frequência que isso).
"""
from __future__ import annotations
import logging
import threading
from pathlib import Path

from app.core import banco

log = logging.getLogger(__name__)

_INTERVALO_SEG = 24 * 60 * 60  # 1x por dia


def _arquivo_de_url(rel: str | None) -> Path | None:
    """Converte a URL relativa gravada em `deteccoes` (ex.: "/static/snapshots/x.jpg")
    no caminho de arquivo real. Só aceita o prefixo esperado — nunca segue caminho
    fora de app/web/static/ mesmo que o valor gravado seja inesperado."""
    if not rel or not rel.startswith("/static/"):
        return None
    return Path("app/web/static") / rel[len("/static/"):]


class RetentionWorker:
    def __init__(self) -> None:
        self._parar = threading.Event()
        self._thread: threading.Thread | None = None
        self._dias = 0

    def iniciar(self, dias: int) -> None:
        if dias <= 0:
            log.info("Retenção de dados desativada (retencao_dias=0) — "
                     "deteccoes/chamadas/JPEGs crescem sem limite")
            return
        self._dias = dias
        self._parar.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="alpr-retencao")
        self._thread.start()
        log.info("Retenção de dados ativa: apaga deteccoes/chamadas com mais de %d dias (checa 1x/dia)", dias)

    def parar(self) -> None:
        self._parar.set()

    def _loop(self) -> None:
        # Roda uma vez já no boot (útil se o servidor ficou dias sem subir) e depois 1x/dia.
        while not self._parar.is_set():
            try:
                self._purgar()
            except Exception as e:
                log.error("Retenção: erro ao purgar dados antigos: %s", e)
            self._parar.wait(_INTERVALO_SEG)

    def _purgar(self) -> None:
        resultado = banco.deteccoes_e_chamadas_antigas(self._dias)
        removidos = 0
        for rel in resultado["arquivos"]:
            caminho = _arquivo_de_url(rel)
            if caminho is None:
                continue
            try:
                caminho.unlink(missing_ok=True)
                removidos += 1
            except OSError as e:
                log.warning("Retenção: falha ao apagar %s: %s", caminho, e)
        if resultado["deteccoes_removidas"] or resultado["chamadas_removidas"]:
            log.info(
                "Retenção: %d detecção(ões) e %d chamada(s) removidas (>%d dias), %d arquivo(s) apagado(s)",
                resultado["deteccoes_removidas"], resultado["chamadas_removidas"], self._dias, removidos,
            )


retencao = RetentionWorker()
