"""Retenção de dados: mantém `deteccoes`/`chamadas` e os JPEGs sob controle.

Sem isso, um servidor multi-tenant rodando meses/anos acumula linha e imagem para
sempre — disco e tabela crescem sem limite, silenciosamente.

São DUAS políticas independentes, e elas apagam coisas diferentes de propósito:

  por PRAZO (`retencao_dias`)         apaga a linha INTEIRA e os arquivos dela. É a
                                      política de privacidade/LGPD, com prazo por posto
                                      (`empresas.retencao_dias_override`).

  por CONTAGEM (`retencao_max_imagens`) apaga só a FOTO das leituras que passaram do teto;
                                      a linha fica. É a política de DISCO: medido em
                                      27/08/2026, 971 leituras ocupavam 221 MB em imagem
                                      contra ~200 KB de banco. O prazo sozinho não segura
                                      isso — deixa crescer 90 dias antes da primeira
                                      limpeza.
"""
from __future__ import annotations
import logging
import threading
import time
from pathlib import Path

from app.core import banco, rotulos, threads

log = logging.getLogger(__name__)

# O teto de contagem precisa de tick curto: a 1x/dia ele permitiria um dia inteiro de
# estouro (232 leituras no pico medido) antes de agir. O prazo em dias continua 1x/dia —
# é medido em dias, não faz sentido checar mais que isso.
_INTERVALO_SEG = 5 * 60
_INTERVALO_DIAS_SEG = 24 * 60 * 60


def _arquivo_de_url(rel: str | None) -> Path | None:
    """Converte a URL relativa gravada em `deteccoes` (ex.: "/static/snapshots/x.jpg")
    no caminho de arquivo real. Só aceita o prefixo esperado — nunca segue caminho
    fora de app/web/static/ mesmo que o valor gravado seja inesperado.

    Defesa em profundidade: além do prefixo, resolve o caminho final e confirma que
    ele continua dentro do diretório base. Hoje `rel` só vem de fontes internas
    controladas (nunca de entrada de usuário), mas caso algum caminho futuro passe a
    gravar esses campos com dado menos controlado (ex.: "/static/../../etc/passwd"),
    a purga não deve apagar nada fora do diretório esperado."""
    if not rel or not rel.startswith("/static/"):
        return None
    base = Path("app/web/static").resolve()
    caminho = (base / rel[len("/static/"):]).resolve()
    try:
        caminho.relative_to(base)
    except ValueError:
        log.warning("Retenção: caminho fora de app/web/static/ ignorado: %s", rel)
        return None
    return caminho


def apagar_orfaos(rels: list[str]) -> int:
    """Apaga do disco JPEGs que nenhuma linha referencia mais. Devolve quantos saíram.

    Existe como função pública porque três caminhos produzem órfão — a purga aqui, a
    remoção manual de uma detecção (`DELETE /api/deteccoes/{id}`) e a absorção da linha do
    pipeline pela leitura reativa (`leitura._mesclar_com_historico`) — e órfão é pior do
    que espaço ocupado: `banco.imagens_excedentes` ancora o teto de contagem no BANCO,
    então um arquivo sem linha é invisível para toda limpeza automática e fica para sempre.

    Não consulta `rotulos.protegidos()` de propósito: os dois chamadores fora daqui apagam
    arquivo que acabou de perder a linha (o da absorção vive no máximo um cooldown, ~120 s),
    e nesse intervalo não existe rótulo humano a proteger. Quem apaga em LOTE por política
    — `_purgar_por_contagem` — faz a checagem de rótulo por conta própria, antes de chamar.
    """
    removidos = 0
    for rel in rels:
        caminho = _arquivo_de_url(rel)
        if caminho is None:
            continue
        try:
            caminho.unlink(missing_ok=True)
            removidos += 1
        except OSError as e:
            log.warning("Retenção: falha ao apagar %s: %s", caminho, e)
    return removidos


class RetentionWorker:
    def __init__(self) -> None:
        self._parar = threading.Event()
        self._thread: threading.Thread | None = None
        self._dias = 0
        self._max_imagens = 0

    def iniciar(self, dias: int, max_imagens: int = 0) -> None:
        # O worker roda SEMPRE, mesmo com o prazo padrão desligado (`dias<=0`): uma
        # empresa pode ter prazo próprio (`empresas.retencao_dias_override`, LGPD por
        # cliente — ver banco.deteccoes_e_chamadas_antigas) que precisa continuar sendo
        # respeitado independente da política padrão do servidor.
        if dias <= 0:
            log.info("Retenção padrão desativada (retencao_dias=0). Deteccoes/chamadas "
                     "sem prazo próprio crescem sem limite; prazos por cliente continuam valendo")
        self._dias = dias
        self._max_imagens = max_imagens
        if max_imagens <= 0:
            log.info("Teto de imagens desativado (retencao_max_imagens=0). O histórico "
                     "guarda foto de toda leitura até o prazo em dias alcançá-la")
        self._parar.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="alpr-retencao")
        self._thread.start()
        log.info("Retenção de dados ativa: padrão >%d dias (0 = desativado, checa 1x/dia); "
                 "foto nas %d leituras mais recentes (0 = sem teto, checa a cada %ds)",
                 dias, max_imagens, _INTERVALO_SEG)

    def parar(self, timeout: float = 10.0) -> bool:
        """Sinaliza parada e espera a purga em andamento terminar. False no timeout.

        Sem o join, o processo pode sair no meio de um `_purgar`: o banco fica com a
        transação revertida (o `cursor()` cuida disso) mas os `unlink` já feitos não voltam,
        e a próxima subida encontra linha apontando para arquivo que não existe mais.
        """
        self._parar.set()
        return threads.encerrar_thread(self._thread, timeout, lambda: log.warning(
            "Retenção: purga não encerrou em %.0fs, seguindo com o desligamento", timeout))

    def _loop(self) -> None:
        # As duas políticas rodam já no boot (útil se o servidor ficou dias sem subir) e
        # depois em cadências próprias — ver `_INTERVALO_SEG`.
        proxima_dias = 0.0
        while not self._parar.is_set():
            agora = time.monotonic()
            if agora >= proxima_dias:
                proxima_dias = agora + _INTERVALO_DIAS_SEG
                try:
                    self._purgar()
                except Exception as e:
                    log.error("Retenção: erro ao purgar dados antigos: %s", e)
            try:
                self._purgar_por_contagem()
            except Exception as e:
                log.error("Retenção: erro ao aplicar o teto de imagens: %s", e)
            self._parar.wait(_INTERVALO_SEG)

    def _purgar(self) -> None:
        resultado = banco.deteccoes_e_chamadas_antigas(self._dias)
        removidos = apagar_orfaos(resultado["arquivos"])
        if resultado["deteccoes_removidas"] or resultado["chamadas_removidas"]:
            log.info(
                "Retenção: %d detecção(ões) e %d chamada(s) removidas (>%d dias), %d arquivo(s) apagado(s)",
                resultado["deteccoes_removidas"], resultado["chamadas_removidas"], self._dias, removidos,
            )

    def _purgar_por_contagem(self) -> None:
        """Tira a foto das leituras que passaram do teto — a linha do histórico fica.

        A ORDEM aqui é o ponto. A proteção de rótulo é consultada ANTES de o banco ser
        tocado: se o dataset estiver ilegível, nada acontece, nem no banco nem em disco.
        Fazer ao contrário — anular a coluna e só então descobrir que não dá para apagar —
        produziria arquivo órfão que nenhuma limpeza futura alcança, porque toda limpeza
        automática parte do banco.

        A checagem de `contagem_com_imagem()` vem ANTES até da proteção de rótulo, e não
        depois: ela é só leitura (nenhuma mutação a proteger), então adiantá-la evita pagar
        `rotulos.protegidos()` (disco + parse de JSON) neste laço que roda a cada 5 minutos
        — na maioria das voltas, em regime estável, não há nada a purgar. (Achado do review
        de 28/08/2026.)
        """
        if self._max_imagens <= 0:
            return
        if banco.contagem_com_imagem() <= self._max_imagens:
            return

        intocaveis = rotulos.protegidos()
        if intocaveis is None:
            # `protegidos` já logou o motivo. Não apagar nada é recuperável; apagar rótulo
            # humano não é — a pasta de snapshots é gitignored e não tem cópia.
            return

        resultado = banco.imagens_excedentes(self._max_imagens)
        if not resultado["leituras_afetadas"]:
            return

        # A foto rotulada SAI do histórico (a coluna já virou NULL) mas FICA em disco: dali
        # em diante ela não é mais registro de operação, é insumo de dataset.
        poupados = [r for r in resultado["arquivos"] if Path(r).name in intocaveis]
        apagaveis = [r for r in resultado["arquivos"] if Path(r).name not in intocaveis]
        removidos = apagar_orfaos(apagaveis)

        # INFO e não DEBUG: apagar imagem é efeito colateral visível, e quem investiga
        # "cadê a foto da leitura de terça" precisa achar isto no log.
        log.info(
            "Teto de imagens: %d leitura(s) perderam a foto para manter as %d mais recentes "
            "(%d arquivo(s) apagado(s), %d poupado(s) por estarem no dataset); as linhas "
            "continuam no histórico",
            resultado["leituras_afetadas"], self._max_imagens, removidos, len(poupados),
        )


retencao = RetentionWorker()
