"""Lógica de domínio da leitura de placa sob demanda ("Ler Placa" / GET reativo).

Reusado por dois endpoints: o reativo multi-tenant (GET /api/leitura, por
entidade/cnpj/automacao/bico) e o teste manual por bico (POST /api/bicos/{id}/
ler-placa-teste, usado pela tela do posto e pelo editor de áreas) — mesmo loop
reject-retry nos dois casos, só muda como a câmera/ROI são resolvidas antes de
chamar ler_placa().

Não importa nada de app/web/ (regra de dependência do projeto: visao importa só core).
Levanta LeituraError em vez de HTTPException — cada rota HTTP converte pro código que
fizer sentido no seu contexto.
"""
from __future__ import annotations
import logging
import re
import sqlite3
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from app.core import banco
from app.core import estado
from app.operacao import retencao as ret_mod
from app.visao import contexto_log
from app.visao import camera as camera_mod
from app.visao.camera import Camera
# Alias com underscore: `confirmada` é o nome da variável local em `ler_placa`.
from app.visao.consenso import confirmada as _confirmada
from app.visao.consenso import (
    acordo_por_caractere, agrupar_por_veiculo, consenso_caractere, leitura_real_proxima,
    prior_de_formato,
)
from app.visao.detector import deslocar, origem_de_bbox
from app.visao.pipeline import _expandir_bbox
from app.visao.validador import parecidas, validar
from app.visao import feira as feira_mod

log = logging.getLogger(__name__)

SNAPSHOT_DIR = Path("app/web/static/snapshots")

# Fora de app/web/static/ DE PROPÓSITO — não é servido por StaticFiles nem cai no bypass
# de autenticação de `/static/` (app/servidor.py:_AuthMiddleware). O preview de um bico
# só sai pela rota autenticada GET /api/bicos/{bico_id}/preview.jpg (app/web/api.py), que
# checa `deps.checar_acesso_empresa` antes de devolver o arquivo.
#
# Motivo: `bico_id` é um inteiro sequencial pequeno COMPARTILHADO entre TODOS os clientes
# do servidor (não há particionamento por tenant no nome do arquivo). Enquanto o preview
# morava em app/web/static/snapshots/preview_bico_{id}.jpg, bastava iterar `_1.jpg`,
# `_2.jpg`, `_3.jpg`... sem autenticação nenhuma para ver a foto (com placa) mais recente
# de qualquer bomba de qualquer posto — vazamento encontrado em 13/08/2026.
#
# Os demais arquivos gravados em SNAPSHOT_DIR (`{ts}_{PLACA}.jpg`, `{ts}_{PLACA}_frame.jpg`,
# gravados mais abaixo, e os do pipeline contínuo em app/visao/pipeline.py) CONTINUAM em
# app/web/static/snapshots/ — mover esses também exigiria trocar o esquema de URL gravado
# em `deteccoes.snapshot`/`.frame` (consumido pelo histórico) e o que `app/visao/pipeline.py`
# grava (fora do escopo desta correção); exigem timestamp+placa para adivinhar, bem menos
# trivial que iterar um inteiro pequeno. Risco residual, registrado conscientemente.
PREVIEW_DIR = Path("app/web/dados_privados/snapshots")


# ── Perfis de leitura ────────────────────────────────────────────────────────────────
# Dois pontos de operação da MESMA leitura, não dois algoritmos. O que muda é o par de
# modelos e o orçamento do laço; a eleição da placa, o consenso e o limiar de `confirmada`
# são idênticos — ver `_ler_placa`.
PERFIL_COMPLETO = "completo"
PERFIL_RAPIDO = "rapido"

# Chaves de orçamento que o perfil rápido sobrescreve, e o prefixo onde ele as procura.
# Tabela em vez de `if perfil == ...` espalhado: cada chave nova de orçamento entra aqui e
# passa a valer nos dois perfis de uma vez.
_CFG_RAPIDO = {
    "snapshots_votacao":      "rapido_snapshots_votacao",
    "leitura_max_tentativas": "rapido_max_tentativas",
    "leitura_timeout_seg":    "rapido_timeout_seg",
}

# A espera pelo primeiro frame no perfil rápido mora em `config.PADROES`, e é lida de lá
# em vez de repetida aqui: um segundo literal com o mesmo número vira duas fontes de
# verdade que ninguém lembra de manter iguais — e a divergência só apareceria num cfg
# incompleto, que é justamente o caso raro em que ninguém está olhando.


def _cfg_perfil(cfg: dict, perfil: str, chave: str, padrao: str) -> str:
    """Valor de `chave` respeitando o perfil: no rápido, a versão `rapido_*` quando existe.

    Cai no valor do perfil completo se a chave rápida não estiver na config — bancos e
    `config.txt` de instalações antigas não a têm, e o modo simplesmente não fica mais
    rápido nesse eixo em vez de explodir.
    """
    if perfil == PERFIL_RAPIDO and chave in _CFG_RAPIDO:
        valor = cfg.get(_CFG_RAPIDO[chave])
        if valor not in (None, ""):
            return str(valor)
    return str(cfg.get(chave, padrao))


def espera_frame_do_perfil(cfg: dict, perfil: str, padrao_completo: float) -> float:
    """Quanto esperar pelo PRIMEIRO frame do pipeline neste perfil.

    É a maior economia isolada do modo rápido, e a menos óbvia: essa espera acontece na
    sondagem (`_sondar_pipeline`), ANTES do laço, então `leitura_timeout_seg` não a
    alcança — uma chamada podia gastar 20 s aqui e só então começar a contar o orçamento.
    """
    from app.core import config

    if perfil != PERFIL_RAPIDO:
        return padrao_completo
    padrao = config.PADROES["rapido_espera_frame_seg"]
    try:
        return float(cfg.get("rapido_espera_frame_seg") or padrao)
    except (TypeError, ValueError):
        return float(padrao)


def _componentes_do_perfil(perfil: str):
    """(fábrica de detector, lock do detector, fábrica de OCR, lock do OCR) deste perfil.

    Import tardio porque `app.visao.detector`/`app.visao.ocr` carregam pilha nativa pesada
    — o módulo de leitura é importado pela camada web no boot, muito antes de existir
    leitura para fazer. Os dois perfis têm instâncias E locks separados: ver o comentário
    de `obter_detector_rapido` sobre por que compartilhar qualquer um dos dois anularia o
    modo.
    """
    from app.visao.detector import (obter_detector_leitura, detector_leitura_lock,
                                    obter_detector_rapido, detector_rapido_lock)
    from app.visao.ocr import (obter_ocr_leitura, ocr_leitura_lock,
                               obter_ocr_rapido, ocr_rapido_lock)
    if perfil == PERFIL_RAPIDO:
        return (obter_detector_rapido, detector_rapido_lock,
                obter_ocr_rapido, ocr_rapido_lock)
    return (obter_detector_leitura, detector_leitura_lock,
            obter_ocr_leitura, ocr_leitura_lock)


def caminho_preview_bico(bico_id: int, camera_db_id: int | None = None) -> Path:
    """Caminho do preview mais recente deste bico (sobrescrito a cada leitura).

    Nome segue exatamente o `preview_nome` que os dois chamadores de `ler_placa`
    (app/web/leitura.py e app/web/cadastro.py) sempre passam: `f"preview_bico_{bico_id}"`.
    Usado pela rota autenticada em app/web/api.py para servir o arquivo com controle de
    acesso — ver o comentário de `PREVIEW_DIR` acima.

    Sem `camera_db_id`: o preview CANÔNICO, o quadro de onde saiu a placa eleita — é o que
    `frame_url` aponta e o que o roteador sempre recebeu. Com `camera_db_id`: o quadro
    daquela câmera específica, que só existe em bico de duas câmeras e serve para o
    operador conferir o enquadramento das duas de uma vez.
    """
    if camera_db_id is None:
        return PREVIEW_DIR / f"preview_bico_{bico_id}.jpg"
    return PREVIEW_DIR / f"preview_bico_{bico_id}_cam{camera_db_id}.jpg"


@dataclass
class EspecificacaoCamera:
    camera_tipo: str
    camera_indice: str
    intelbras_host: str
    intelbras_porta: str
    intelbras_usuario: str
    intelbras_senha: str
    intelbras_canal: str
    intelbras_subtype: str
    intelbras_formato: str
    rtsp_url_custom: str = ""

    @classmethod
    def de_camera_db(cls, cam: dict, cfg: dict) -> "EspecificacaoCamera":
        return cls(
            camera_tipo=cam["camera_tipo"],
            camera_indice=cam.get("camera_indice", "0"),
            intelbras_host=cam.get("intelbras_host", ""),
            intelbras_porta=cam.get("intelbras_porta", "554"),
            intelbras_usuario=cam.get("intelbras_usuario", "admin"),
            intelbras_senha=cam.get("intelbras_senha") or cfg.get("intelbras_senha", ""),
            intelbras_canal=cam.get("intelbras_canal", "1"),
            intelbras_subtype=cam.get("intelbras_subtype", "1"),
            intelbras_formato=cam.get("intelbras_formato", "padrao"),
            rtsp_url_custom=cam.get("rtsp_url_custom", ""),
        )


class LeituraError(Exception):
    def __init__(self, status: int, mensagem: str):
        super().__init__(mensagem)
        self.status = status
        self.mensagem = mensagem


class BancoIndisponivelError(LeituraError):
    """`sqlite3.OperationalError` ("database is locked") que sobreviveu a `_com_retry_lock`.

    Subclasse de `LeituraError` DE PROPÓSITO: o `except leitura.LeituraError` que já
    existe em `app/web/leitura.py:leitura_reativa` passa a cobrir este caso também, sem
    precisar de um segundo bloco `except` — a tentativa continua sendo registrada em
    `chamadas` (auditoria) antes do erro HTTP ser devolvido ao roteador, exatamente como
    já acontece hoje para falha de câmera/OCR. Sem essa subclasse, o `OperationalError`
    cru subia direto do `_ler_placa`, pulava o único `except` da rota, e a tentativa
    nunca virava linha na tabela `chamadas` — falha silenciosa no fluxo de cobrança.
    """


# Backoff entre tentativas de escrita quando o banco está com lock passageiro (a purga
# diária de retenção, app/operacao/retencao.py, pode segurar uma transação de escrita
# além do busy_timeout de 10s — app/core/banco/_base.py). Curto de propósito: é só para
# o caso comum de a transação concorrente liberar o lock enquanto esperamos — não é uma
# tentativa de esperar o `busy_timeout` (10s) inteiro de novo em loop.
_RETRY_BACKOFF_SEG: tuple[float, ...] = (0.2, 0.5, 1.0)


def _com_retry_lock(operacao: Callable[[], object], contexto: str) -> object:
    """Executa uma escrita de banco tolerando "database is locked" passageiro.

    Só absorve ESSE erro específico (mensagem contém "locked", case-insensitive) — até
    `len(_RETRY_BACKOFF_SEG)` tentativas extras, além da primeira. Qualquer outra
    exceção (inclusive outro `sqlite3.OperationalError`, ex.: "no such table") sobe na
    hora, sem mascarar bug nenhum. Se todas as tentativas se esgotarem, levanta
    `BancoIndisponivelError` — nunca finge sucesso.
    """
    ultimo_erro: sqlite3.OperationalError | None = None
    total_tentativas = 1 + len(_RETRY_BACKOFF_SEG)
    for tentativa, espera in enumerate((0.0, *_RETRY_BACKOFF_SEG), start=1):
        if espera:
            time.sleep(espera)
        try:
            return operacao()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower():
                raise
            ultimo_erro = e
            log.warning("Banco travado ao %s (tentativa %d/%d): %s",
                        contexto, tentativa, total_tentativas, e)
    raise BancoIndisponivelError(
        503, f"Banco de dados indisponível ao {contexto} — tente novamente em instantes.",
    ) from ultimo_erro


# Re-exportado de `consenso.py`, onde a regra passou a morar para o TRACKER também
# alcançá-la (ver o docstring daquele módulo). O alias privado fica porque este nome é o
# que `testes/unitarios/test_consenso.py` importa e o que o resto deste arquivo usa.
_consenso_caractere = consenso_caractere


def _acordo_metrica(cfg: dict | None) -> str:
    """'string' (default) ou 'caractere' — como `acordo` e medido. Ver `acordo_metrica`."""
    if not cfg:
        return "string"
    return "caractere" if str(cfg.get("acordo_metrica", "string")).strip().lower() == "caractere" else "string"


def _eleger_placa(candidatos: list[dict], metrica: str = "string",
                  leituras_extra: list[tuple[str, float]] | None = None,
                  com_alternativa: bool = False) -> dict | None:
    """Elege a placa final por consenso de caractere entre TODOS os candidatos acumulados.

    Reusada tanto pela checagem de parada antecipada do loop de leitura (a cada frame)
    quanto pela decisão final — garante que o resultado não muda dependendo de quando o
    loop parou, só a quantidade de evidência acumulada até ali.
    Retorna None se `candidatos` estiver vazio. O dict retornado inclui as chaves extras
    "acordo" (concordância 0-1 da placa eleita) e "n_votos_snap" (fotos que bateram nela).
    """
    if not candidatos:
        return None

    # Pool de leituras: a placa final de cada candidato + cada engine individual,
    # ponderadas por confiança. Vota-se cada posição de caractere separadamente.
    leituras: list[tuple[str, float]] = []
    for c in candidatos:
        leituras.append((c["placa"], float(c["confianca"])))
        for d in c.get("detalhes_ocr", []):
            if d.get("placa"):
                leituras.append((d["placa"], float(d.get("confianca", 0.5))))


    # Vota só entre leituras do MESMO veículo. O pool mistura as câmeras do bico, e com duas
    # câmeras a da frente costuma enquadrar outro veículo (moto não tem placa dianteira) —
    # fundir posição a posição leituras de veículos diferentes não corrige ruído, fabrica
    # placa. Ver `agrupar_por_veiculo`.
    grupos = agrupar_por_veiculo(leituras)
    pool = grupos[0] if grupos else leituras

    # Leituras que o monitoramento continuo ja fez. Entram DEPOIS de o grupo estar escolhido,
    # e so as que pertencem a ele.
    #
    # A ordem importa e foi um bug: injetadas junto com as outras, elas disputavam a escolha
    # do grupo e PODIAM GANHAR - duas leituras do continuo a 0,95 pesam mais que a unica foto
    # do GET a 0,90, e a chamada emitia a placa do carro do bico ao lado. Quem esta no bico
    # AGORA e definido pelas fotos DESTA chamada; o continuo so refina os caracteres da placa
    # que elas ja identificaram.
    #
    # Peso a 70% pelo mesmo motivo: e evidencia boa, de segundos atras, mas de outro instante.
    # `max_diff=3` e nao 2, e o numero nao e novo: e o mesmo que `_mesclar_com_historico`
    # usa para decidir "esta leitura e o MESMO veiculo daquela de instantes atras". A
    # pergunta aqui e identica, e a distancia tipica e maior do que dentro de uma chamada
    # so - no caso do bico 3, `HDX2477` (foto do GET) e `RLX2A77` (leitura do continuo)
    # estao a 3 caracteres, enquanto o carro do bico ao lado fica a 6-7. O 2 de
    # `agrupar_por_veiculo` continua valendo para agrupar leituras do MESMO instante.
    if leituras_extra:
        pool = pool + [(pt, float(ct) * 0.7) for pt, ct in leituras_extra
                       if pt and len(pt) == 7
                       and any(parecidas(pt, q, 3) for q, _ in pool)]

    # Prior de formato do POOL que vai votar (ver `consenso.prior_de_formato`, que explica
    # por que nao pode sair dos `candidatos` nem ser contado sem peso). O fallback para o
    # `padrao` dos candidatos cobre o pool em que nada valida.
    formato_prior = prior_de_formato(pool)
    if formato_prior is None:
        fmt_cands = Counter(c["padrao"] for c in candidatos if c.get("padrao"))
        formato_prior = fmt_cands.most_common(1)[0][0] if fmt_cands else None

    placa_consenso = _consenso_caractere(pool, formato=formato_prior)
    votos_placa = Counter(c["placa"] for c in candidatos)
    # `leitura_real_proxima` é a segunda tranca: a string montada caractere a caractere só
    # vale se tiver respaldo em algo que um engine de fato leu. Sem isto, `OSL2G55` + um
    # candidato de outro veículo emitia `OSL2855` — que ninguém leu — com acordo 0,00.
    if (placa_consenso and validar(placa_consenso)
            and leitura_real_proxima(placa_consenso, pool)):
        placa_eleita = placa_consenso           # consenso por caractere (corrige 1-char)
    else:
        placa_eleita = votos_placa.most_common(1)[0][0]   # fallback: string mais votada

    n_votos_snap = sum(1 for c in candidatos if c["placa"] == placa_eleita)
    # LEITURAS que apoiam a placa eleita, que e coisa diferente de FOTOS que bateram com ela.
    # Com o ensemble, uma foto rende 3-4 leituras de modelos diferentes; a regra dos 2 votos
    # foi escrita quando uma foto valia uma leitura, e o docstring de `consenso.confirmada`
    # diz por que ela existia: "uma fracao sobre uma amostra de tamanho 1 vale 1.0 sem que
    # nada tenha concordado com nada". Com N modelos lendo o MESMO recorte, a amostra deixou
    # de ser 1 e a justificativa mudou de lugar.
    #
    # Criterio `parecidas(..., 2)`, o mesmo que `ocr/auto.py::_fundir` ja usa para calcular a
    # confianca: leitura a 1-2 caracteres da eleita e ruido de OCR sobre a MESMA placa, e
    # conta como apoio; 3+ e outra placa e nao conta.
    #
    # Medido em 80 recortes de placa real contra 80 falsos positivos do detector:
    #   >=2 leituras apoiando E acordo >=0,80  ->  86% das reais passam, 4% dos falsos passam
    # Contra 0% e 0% da regra por fotos, que com 1 foto no orcamento nunca fecha.
    #
    # Conta sobre `detalhes_ocr` (uma entrada por ENGINE) e NAO sobre `pool`: o pool recebe a
    # placa final de cada candidato MAIS cada engine dele, e a placa final e derivada dos
    # engines - ela entra duas vezes. Medido: um falso positivo em que UM unico engine
    # validou dava `n_votos_leitura = 2` e passava a guarda dos 2, anulando exatamente os 4%
    # de falso aceite que a regra foi calibrada para ter.
    #
    # Candidato SEM `detalhes_ocr` conta como UMA leitura (a dele), e nao zero: e o caso do
    # engine que nao reporta detalhe por modelo, dos dubles de teste e de qualquer caminho
    # que nao passe pelo ensemble. Zero ali tornaria `confirmada` inalcancavel nesses
    # caminhos - trocaria um bug por outro, na direcao oposta.
    n_votos_leitura = 0
    for c in candidatos:
        det = [d for d in c.get("detalhes_ocr", []) if d.get("placa")]
        if det:
            n_votos_leitura += sum(1 for d in det
                                   if parecidas(d["placa"], placa_eleita, 2))
        elif c.get("placa") and parecidas(c["placa"], placa_eleita, 2):
            n_votos_leitura += 1
    # Melhor candidato (p/ crop/bbox): o da placa eleita, senão o de maior confiança
    cands_eleita = [c for c in candidatos if c["placa"] == placa_eleita]
    melhor = dict(max(cands_eleita or candidatos, key=lambda c: c["confianca"]))
    melhor["placa"] = placa_eleita
    _v = validar(placa_eleita)
    if _v:
        melhor["padrao"] = _v[1]

    # Concordância: fração do peso das leituras que bateu com a placa eleita — usada tanto
    # para escalar a confiança final quanto como sinal de parada antecipada do loop.
    #
    # Denominador é o pool INTEIRO, não o grupo que votou, e isto é deliberado: quando as
    # duas câmeras do bico enxergam veículos diferentes, o acordo CAI e a leitura sai
    # "a conferir". É o sinal honesto para o atendente — a placa passa a estar certa (o
    # grupo é que decide) e continua marcada, em vez de errada e marcada como antes.
    # Trocar para o grupo INFLA o acordo e é o que `acordo_metrica` vai medir em produção
    # antes de virar default; não mudar aqui sem recalibrar `leitura_acordo_minimo`.
    def _acordo_string() -> float:
        peso_total = sum(w for _, w in leituras)
        return sum(w for p, w in leituras if p == placa_eleita) / max(peso_total, 1e-6)

    if metrica == "caractere":
        # Escala nova: concordancia media por POSICAO, dentro do grupo que votou. Mede o que
        # a fusao faz, mas move o ponto de corte de `leitura_acordo_minimo` - por isso so
        # entra quando pedida explicitamente.
        acordo = acordo_por_caractere(placa_eleita, pool)
    else:
        acordo = _acordo_string()
    melhor["confianca"] = round(melhor["confianca"] * max(acordo, 0.34), 3)
    melhor["acordo"] = round(acordo, 3)
    # A OUTRA metrica, so quando pedida (`leitura_log_parcial`): serve para responder, com
    # o MESMO pool e a MESMA placa eleita, se `acordo_metrica=caractere` resolveria os casos
    # em que a leitura esta certa e o acordo nao fecha. Sem isso seria preciso uma segunda
    # campanha, e a coleta depende do movimento do posto — o recurso escasso aqui.
    #
    # Nao entra no payload nem no banco: e instrumentacao, e quem consome `acordo` tem de
    # ver UM numero so, o da metrica configurada.
    #
    # As duas escalas NAO olham o mesmo conjunto, e isso e deliberado (ver o comentario
    # acima): `string` mede sobre `leituras` (pool inteiro, inclusive o que o agrupamento
    # descartou) e `caractere` sobre `pool` (so o grupo vencedor). Trocar a escala sem
    # recalibrar `leitura_acordo_minimo` move o corte de todas as leituras do posto.
    if com_alternativa:
        melhor["acordo_alt"] = round(
            _acordo_string() if metrica == "caractere"
            else acordo_por_caractere(placa_eleita, pool), 3)
    melhor["n_votos_snap"] = n_votos_snap
    melhor["n_votos_leitura"] = n_votos_leitura
    return melhor


def _mesclar_com_anterior(melhor: dict, anterior: dict) -> dict:
    """Combina a placa desta leitura com a última detecção do MESMO bico, ainda dentro
    do cooldown, por consenso de caractere.

    O roteador costuma acionar o mesmo bico várias vezes seguidas para o mesmo veículo
    (reflexo do bico, retry dele). `_eleger_placa` só vê as fotos de UMA chamada — aqui
    o resultado já votado da chamada anterior entra como mais uma leitura, do jeito que
    entraria se tivesse vindo do mesmo loop. Sem isso, ruído de 1-2 caracteres entre
    chamadas sucessivas virava uma segunda linha diferente no histórico para o mesmo carro.
    """
    leituras = [(melhor["placa"], melhor["confianca"]), (anterior["placa"], anterior["confianca"])]
    consenso = _consenso_caractere(leituras, formato=melhor.get("padrao"))
    if consenso and validar(consenso):
        placa_final = consenso
    else:
        placa_final = melhor["placa"] if melhor["confianca"] >= anterior["confianca"] else anterior["placa"]

    fundido = dict(melhor)
    fundido["placa"] = placa_final
    v = validar(placa_final)
    if v:
        fundido["padrao"] = v[1]
    fundido["confianca"] = round(max(melhor["confianca"], anterior["confianca"]), 3)
    # Herda o BLOCO inteiro (tipo + sinal cru) quando esta leitura não conseguiu estimar.
    # A leitura reativa recorta pela ROI do bico, então o estágio de veículo às vezes vê só
    # um pedaço do carro e devolve None, enquanto a linha absorvida — vinda do pipeline,
    # que viu o quadro inteiro — costuma ter o tipo. Mesmo raciocínio do CASE WHEN de
    # `atualizar_deteccao`, aplicado ao caminho que APAGA a linha anterior em vez de
    # atualizá-la: os quatro campos se movem juntos, nunca um sem os outros três — senão
    # `tipo_veiculo` herdaria do anterior enquanto `veiculo_classe` ficaria da leitura
    # atual, tipo de um veículo com sinal cru de outro.
    if fundido.get("tipo_veiculo") is None:
        fundido["tipo_veiculo"] = anterior.get("tipo_veiculo")
        fundido["veiculo_classe"] = anterior.get("veiculo_classe")
        fundido["veiculo_conf"] = anterior.get("veiculo_conf")
        fundido["tipo_veiculo_fonte"] = anterior.get("tipo_veiculo_fonte")
    return fundido


def _mesclar_com_historico(
    melhor: dict,
    bico_id: int,
    camera_ids: list[int],
    origem: str,
    cooldown_seg: float,
) -> tuple[dict, int | None]:
    """Cruza esta leitura com o histórico recente e decide se ela é o MESMO veículo.

    Devolve `(melhor, anterior_id)`: com `anterior_id` preenchido, quem chama deve
    ATUALIZAR aquela linha em vez de inserir uma nova — o histórico mostra um evento por
    veículo, não um por chamada do roteador.

    Teste manual (botão da tela de ROI/posto) fica de fora, por duas razões:

    1. Quem está ajustando enquadramento aperta o botão várias vezes de propósito e
       precisa ver uma linha por aperto — mesclar esconderia exatamente a comparação
       entre tentativas que motivou o teste.
    2. Mais grave: o ramo `else` abaixo ABSORVE a detecção do 'pipeline' e APAGA a linha
       original. Essa absorção foi escrita para a leitura do roteador, que é o evento com
       significado de negócio; disparada por um teste, ela deletava do histórico uma
       detecção real do monitoramento contínuo só porque alguém foi conferir a câmera.
    """
    if origem == "teste":
        return melhor, None

    desde = (datetime.now(timezone.utc) - timedelta(seconds=cooldown_seg)).isoformat()

    # Mesmo bico, ainda dentro do cooldown, placa parecida: o roteador aciona de novo pro
    # mesmo veículo (retry dele, novo pulso do bico) e sem isto cada chamada emitia sua
    # própria linha, mesmo quando só 1-2 caracteres divergiam por ruído de OCR.
    anterior = banco.ultima_deteccao_bico(bico_id, desde, origem)
    if anterior and parecidas(anterior["placa"], melhor["placa"], max_diff=3):
        return _mesclar_com_anterior(melhor, anterior), anterior["id"]

    # Sem match no mesmo bico: cruza com o 'pipeline' (monitoramento contínuo das MESMAS
    # câmeras físicas, sem bico_id) — o mesmo veículo é comum aparecer nos dois quase ao
    # mesmo tempo. A leitura reativa é o evento com significado de negócio (ligada ao
    # bico), então ABSORVE o 'pipeline' em vez do contrário: some com aquela linha e
    # grava só a reativa.
    #
    # Varre TODAS as câmeras do bico: com duas, o mesmo carro gera uma detecção contínua
    # em cada uma. Absorver só a de uma câmera deixaria a outra órfã no histórico — o
    # mesmo veículo apareceria duas vezes, que é exatamente o que esta regra existe para
    # impedir.
    for cid in camera_ids:
        pipeline_anterior = banco.ultima_deteccao_camera(cid, desde, origem="pipeline")
        if pipeline_anterior and parecidas(pipeline_anterior["placa"], melhor["placa"], max_diff=3):
            melhor = _mesclar_com_anterior(melhor, pipeline_anterior)
            # `_id=` amarra o valor AGORA: sem o argumento padrão, todas as lambdas do
            # laço compartilhariam o mesmo `pipeline_anterior` e apagariam a última linha
            # várias vezes em vez de uma linha por câmera.
            orfaos = _com_retry_lock(
                lambda _id=pipeline_anterior["id"]: banco.remover_deteccao(_id),
                "remover detecção anterior absorvida")
            # A linha absorvida leva os JPEGs dela junto. `_mesclar_com_anterior` parte de
            # `dict(melhor)` e nunca copia `snapshot`/`frame` do anterior, então esses
            # arquivos não são referenciados por mais ninguém — e órfão aqui é invisível
            # para o teto de contagem, que parte do banco.
            if orfaos:
                ret_mod.apagar_orfaos(orfaos)
    return melhor, None


def _detectar(det_inst, frame, roi: dict | None, lock: threading.Lock):
    """Detecta placas no frame, recortando por ROI antes (mesmo padrão de
    Pipeline._processar_frame) quando o bico tem uma área própria configurada.
    """
    if roi:
        rx, ry, rw, rh = roi["x"], roi["y"], roi["w"], roi["h"]
        frame_det = frame[ry:ry + rh, rx:rx + rw]
        if frame_det.size == 0:
            return []
        with lock:
            bboxes_roi = det_inst.detectar(frame_det)
        # `deslocar` e não uma comprehension crua: remontar a tupla descartaria o
        # `tipo_veiculo` que o 2 estágios anexou à bbox.
        return [deslocar(b, rx, ry) for b in bboxes_roi]
    with lock:
        return det_inst.detectar(frame)


# ── Lock por câmera ──────────────────────────────────────────────────────────
# Evita que 2 bicos compartilhando a MESMA câmera abram conexões RTSP simultâneas
# (câmeras Intelbras só toleram 1 conexão por vez). Câmeras diferentes seguem em
# paralelo — o lock é por camera_id.
#
# ATENÇÃO à latência: o lock cobre a leitura INTEIRA (conexão + loop reject-retry),
# não só o connect, porque a conexão RTSP fica aberta durante todo o loop. Logo, dois
# bicos da mesma câmera acionados ao mesmo tempo são serializados: o segundo espera o
# primeiro terminar, e no pior caso a resposta dele leva ~2x `leitura_timeout_seg`.
# Com o padrão de 28s isso estoura a tolerância de ~25-30s do roteador. Mitigações se
# isso aparecer em campo: baixar `leitura_timeout_seg`, ou dar um timeout de aquisição
# ao lock e responder 503 rápido em vez de enfileirar.
#
# ORDEM DE AQUISIÇÃO: um bico com duas câmeras pega DOIS locks, e aí a ordem passa a
# importar. Dois bicos cadastrados com o mesmo par de câmeras em ordens opostas
# (bico A = [3, 4], bico B = [4, 3]) travariam um ao outro para sempre se cada um
# pegasse na ordem da própria lista. `_adquirir_locks` sempre ordena por `camera_id`
# crescente — ordem total sobre o recurso, que é o que elimina o ciclo. Os outros
# detentores destes locks (pipeline.iniciar/reconexão, snapshot do editor de áreas,
# captura de dataset) pegam exatamente UM lock cada e não podem fechar ciclo.
_locks_camera: dict[int, threading.Lock] = {}
_locks_camera_guarda = threading.Lock()


def _obter_lock_camera(camera_id: int) -> threading.Lock:
    with _locks_camera_guarda:
        lock = _locks_camera.get(camera_id)
        if lock is None:
            lock = threading.Lock()
            _locks_camera[camera_id] = lock
        return lock


def lock_camera(camera_id: int) -> threading.Lock:
    """Lock público desta câmera — usado por quem também abre conexão direta (ex.: o
    snapshot do editor de ROI), para não competir com uma leitura em andamento."""
    return _obter_lock_camera(camera_id)


# ── Fontes de imagem de um bico ──────────────────────────────────────────────
# Um bico enxerga o veículo por 1 ou 2 câmeras (traseira/frente). A segunda existe porque
# a câmera fica elevada: com estepe/roda na traseira, a placa traseira não aparece em
# pixel nenhum, e nenhum ajuste de OCR resolve isso — só outro ângulo.
#
# As duas alimentam UM pool de candidatos e UM orçamento de tempo, não duas leituras: o
# consenso (`_eleger_placa`) é agnóstico a de qual câmera veio cada voto, e dois ângulos
# diferentes são evidência mais independente que dois frames seguidos da mesma câmera.
# Duas chamadas separadas custariam 2x `leitura_timeout_seg` (estourando a tolerância do
# roteador) e ainda disputariam a gravação no histórico, duplicando o veículo.

# Rodadas do round-robin antes de poder abandonar uma câmera improdutiva. Duas dá a cada
# câmera pelo menos 2 fotos — o bastante para não descartar uma por causa de um único
# frame borrado, e barato perto do orçamento total.
RODADAS_MINIMAS = 2

# Piso de fotos que o log contrafactual (`leitura_log_parcial`) simula. É 2 e não 1 porque
# 1 é medidamente inseguro: com o ensemble real (3 fast + paddle), `n_votos_leitura >= 2`
# fecha DENTRO da primeira foto, então o laço pararia sem nenhuma segunda foto para
# confirmar — e as 4 leituras são do MESMO recorte, logo concordam sobre um falso positivo
# do detector se houver um. Ver `testes/unitarios/test_parada_antecipada.py`, que fixa isso.
#
# 2 é também o maior valor que não mexe em `confirmada`: `consenso.confirmada` usa
# `min(2, n_min)`, que satura — 2, 3, 4 e 12 são a MESMA regra de confirmação.
PISO_CONTRAFACTUAL = 2


@dataclass
class FonteLeitura:
    """Uma câmera do bico, com tudo que o laço precisa para tirar foto dela."""

    camera_id: int
    papel: str                                  # 'traseira' | 'frente' — rótulo, não regra
    especificacao: EspecificacaoCamera
    roi: dict | None                            # em coordenadas do frame DESTA câmera
    provider: Callable[[], np.ndarray | None] | None = None
    # Leituras que o monitoramento continuo JA fez desta camera. Entram no pool de votacao
    # mas NAO contam como foto desta chamada - ver `_eleger_placa(leituras_extra=...)`.
    leituras_provider: Callable[[], list[tuple[str, float]]] | None = None

    # Preenchidos na abertura (`_abrir_fontes`: sonda o pipeline, pega o lock, conecta)
    usar_pipeline: bool = False
    # True quando a sondagem CHEGOU a chamar o provider do pipeline. Distingue "o pipeline
    # existe e não entregou frame" de "não há pipeline nesta câmera" — os dois deixam
    # `usar_pipeline` False, mas só no primeiro a conexão RTSP direta está condenada
    # (a Intelbras aceita uma conexão só, e o pipeline está com ela). Ver `_abrir_fontes`.
    pipeline_sondado: bool = False
    camera_direta: Camera | None = None
    ajustador: object | None = None
    lock: threading.Lock | None = None
    lock_adquirido: bool = False
    frame_inicial: np.ndarray | None = None
    erro: str | None = None

    # Estado do laço
    ativa: bool = True
    motivo_inativa: str = ""
    tentativas: int = 0
    bboxes: int = 0
    candidatos: int = 0
    ultimo_ts: float = 0.0
    frame_principal: np.ndarray | None = None
    nitidez_principal: float = -1.0

    @property
    def rotulo(self) -> str:
        return f"cam{self.camera_id} ({self.papel})"

    def estado(self) -> str:
        if self.erro is not None:
            return "indisponivel"
        return "abandonada" if not self.ativa else "usada"


def _adquirir_locks(fontes: list[FonteLeitura], espera_seg: float) -> None:
    """Adquire o lock das fontes que abrem conexão direta, em ordem de `camera_id`.

    Com UMA fonte a espera TAMBÉM tem teto (achado M10) — não bloqueia mais indefinidamente
    como antes: os detentores deste lock seguram por muito tempo (coletor de dataset até
    15s, reconexão do pipeline com dois `sleep(5)`), e bloquear aqui comia o orçamento do
    ROTEADOR (28s) antes mesmo do `leitura_timeout_seg` da própria leitura começar a
    contar. Estourando o teto, a fonte sai da leitura em vez de travar — a leitura
    degradada (que pode cair no frame do pipeline, sem abrir RTSP) ainda é melhor que
    devolver erro ao bico.

    Com duas fontes, o mesmo teto por lock evita que um bico de duas câmeras trave um bico
    vizinho que só compartilha a SECUNDÁRIA — mas aqui, se NENHUMA das duas for adquirida
    dentro do teto, volta ao comportamento bloqueante de sempre em vez de desistir de vez:
    uma leitura lenta ainda é melhor que nenhuma fonte disponível. Essa rede de segurança
    não existe no caminho de uma fonte só — lá, estourar o teto já é o fim da tentativa
    para aquela fonte.
    """
    diretas = sorted([f for f in fontes if f.lock is not None], key=lambda f: f.camera_id)
    if not diretas:
        return
    if len(diretas) == 1:
        # Com teto, como o caminho de duas câmeras. Bloquear para sempre aqui era o caso
        # NORMAL (a maioria dos bicos tem uma câmera só), e os outros detentores deste lock
        # seguram por muito tempo: o coletor de dataset cobre `capturar_frame_unico` inteiro
        # (até 15 s esperando o 1º frame + 6 s de join ao fechar) e a reconexão do pipeline
        # faz duas tentativas com `sleep(5)` entre elas. A request ficava ~21 s parada ANTES
        # de começar a contar o próprio `leitura_timeout_seg` de 28 s, e o roteador desistia
        # antes — abastecimento sem placa. O próprio arquivo previa esta mitigação e a tinha
        # aplicado só ao outro ramo. (Auditoria 27/08/2026, achado M10.)
        if diretas[0].lock.acquire(timeout=espera_seg):
            diretas[0].lock_adquirido = True
        else:
            # Segue SEM o lock em vez de abortar: a leitura degradada (que pode cair no
            # frame do pipeline, sem abrir RTSP) ainda é melhor que devolver erro ao bico.
            diretas[0].ativa = False
            diretas[0].erro = "câmera ocupada por outra leitura em andamento"
            log.warning("%s: lock não adquirido em %.1fs — seguindo sem conexão direta",
                        diretas[0].rotulo, espera_seg)
        return

    for f in diretas:
        if f.lock.acquire(timeout=espera_seg):
            f.lock_adquirido = True
        else:
            f.ativa = False
            f.erro = "câmera ocupada por outra leitura em andamento"
            log.warning("%s: lock não adquirido em %.1fs — fonte fora desta leitura",
                        f.rotulo, espera_seg)

    if not any(f.lock_adquirido for f in diretas):
        primeira = diretas[0]
        primeira.lock.acquire()
        primeira.lock_adquirido = True
        primeira.ativa = True
        primeira.erro = None


def _sondar_pipeline(f: FonteLeitura, cfg: dict) -> None:
    """FASE 1 da abertura: descobre se esta fonte pode reusar o frame do pipeline contínuo.

    Roda ANTES de qualquer lock ser adquirido, e é de propósito: só depois de chamar o
    provider se sabe se a fonte vai abrir conexão RTSP própria (o pipeline pode estar
    reconectando e devolver None, caindo para conexão direta). Decidir o lock antes desta
    sondagem deixaria a conexão direta desse caso SEM serialização — e a câmera Intelbras
    aceita uma conexão só, então uma segunda tentativa simplesmente falha.

    NUNCA levanta: o provider é código de rede e um erro dele apenas significa "sem frame
    do pipeline", que tem tratamento (cair para conexão direta).
    """
    deteccao_auto = cfg.get("deteccao_automatica", "sim").lower() in ("sim", "true", "1")
    f.usar_pipeline = f.provider is not None and deteccao_auto
    if not f.usar_pipeline:
        return
    f.pipeline_sondado = True
    try:
        f.frame_inicial = f.provider()
    except Exception as e:
        log.warning("%s: provider do pipeline falhou (%s) — usando conexão direta",
                    f.rotulo, e)
        f.frame_inicial = None
    if f.frame_inicial is None:
        f.usar_pipeline = False      # daqui em diante esta fonte PRECISA de lock


def _abrir_uma(f: FonteLeitura, cfg: dict) -> None:
    """FASE 2 da abertura: abre a conexão RTSP direta desta fonte. NUNCA levanta.

    Só é chamada para fontes que a sondagem marcou como `usar_pipeline = False`, e sempre
    com o lock daquela câmera já adquirido.

    Falha vira `f.erro` + `f.ativa = False`, porque quem decide se a leitura inteira
    fracassou é o orquestrador — com duas câmeras, perder uma é degradação, não erro.
    """
    try:
        if f.usar_pipeline:
            return

        # O pipeline contínuo já aplica AjustadorAmbiente antes de publicar o frame
        # "limpo"; a conexão DIRETA não passava por ajuste nenhum, e é justamente nesses
        # momentos de instabilidade que robustez a iluminação mais importaria.
        from app.visao.ambiente import AjustadorAmbiente
        f.ajustador = AjustadorAmbiente(cfg, camera_db_id=f.camera_id)

        intelbras = {
            "host": f.especificacao.intelbras_host,
            "porta": f.especificacao.intelbras_porta,
            "usuario": f.especificacao.intelbras_usuario,
            "senha": f.especificacao.intelbras_senha,
            "canal": f.especificacao.intelbras_canal,
            "subtype": f.especificacao.intelbras_subtype,
            "formato": f.especificacao.intelbras_formato,
            "rtsp_transporte": cfg.get("rtsp_transporte", "tcp"),
        }
        if f.especificacao.rtsp_url_custom:
            intelbras["host"] = ""
        try:
            f.camera_direta = Camera(
                tipo=f.especificacao.camera_tipo,
                indice=f.especificacao.rtsp_url_custom or f.especificacao.camera_indice,
                largura=int(cfg.get("camera_largura", "1280")),
                altura=int(cfg.get("camera_altura", "720")),
                fps=int(cfg.get("camera_fps", "15")),
                intelbras=intelbras,
            )
            f.camera_direta.abrir()
        except Exception as e:
            log.warning("ler-placa %s: %s", f.rotulo, e)
            host = f.especificacao.intelbras_host or f.especificacao.rtsp_url_custom
            # Remove credenciais de URLs RTSP antes de expor na mensagem de erro
            host_safe = re.sub(r"(rtsp?://)[^@]+@", r"\1***:***@", host)
            detalhe = f" ({host_safe})" if host_safe else ""
            f.erro = (f"não foi possível conectar via RTSP{detalhe} — verifique IP/host, "
                      "porta, usuário e senha")
            f.ativa = False
            return

        # Aguarda o primeiro frame válido (até 15s)
        for _ in range(150):
            f.frame_inicial = f.camera_direta.ler()
            if f.frame_inicial is not None:
                break
            time.sleep(0.1)
        if f.frame_inicial is None:
            # Câmera que conectou e não mandou frame é o caso MAIS provável de leitora
            # presa em `cap.read()` — `fechar_ou_adiar` retém a instância em vez de
            # deixar o coletor de lixo liberar o cap por baixo da leitura viva
            # (access violation, ver app/visao/camera.py).
            camera_mod.fechar_ou_adiar(f.camera_direta, f"ler-placa {f.rotulo}")
            f.camera_direta = None
            f.erro = "câmera conectou mas não enviou frames"
            f.ativa = False
            return
        if f.ajustador is not None and f.ajustador.ativo:
            f.frame_inicial = f.ajustador.processar(f.frame_inicial)
    except Exception as e:                       # rede/driver imprevisível: degrada, não derruba
        log.warning("ler-placa %s: falha inesperada ao abrir: %s", f.rotulo, e)
        f.erro = f"falha ao abrir a câmera: {e}"
        f.ativa = False


def _em_paralelo(fontes: list[FonteLeitura], etapa, cfg: dict) -> None:
    """Roda `etapa(fonte, cfg)` em todas as fontes; com mais de uma, EM PARALELO.

    Não é otimização, é requisito de orçamento: em série, o `provider` de cada fonte pode
    esperar até 20s pelo primeiro frame de um pipeline aquecendo (e uma conexão RTSP nova
    custa 2-3s). Duas fontes em série já estouram sozinhas os 28s da chamada, sem ter
    analisado uma única foto. É espera de I/O pura — a inferência continua serializada
    pelos locks globais de detector/OCR, e nada aqui os toca.
    """
    if not fontes:
        return
    if len(fontes) == 1:
        etapa(fontes[0], cfg)          # inline: sem thread, rastro de pilha igual ao de sempre
        return

    # `contexto_log` vive em threading.local e NÃO é herdado por thread nova — sem
    # capturar/herdar, tudo que a abertura logar sai sem dono no arquivo compartilhado
    # com os pipelines contínuos.
    ctx = contexto_log.capturar()

    def _com_contexto(f: FonteLeitura) -> None:
        with contexto_log.herdar(ctx), contexto_log.usar(camera=f.camera_id):
            etapa(f, cfg)

    with ThreadPoolExecutor(max_workers=len(fontes)) as executor:
        # As etapas não levantam, mas o `.result()` de todos é obrigatório: uma thread
        # abandonada terminaria depois com uma conexão RTSP aberta e um lock preso para
        # sempre. Os tetos de tempo moram DENTRO de cada worker, nunca neste join.
        for fut in [executor.submit(_com_contexto, f) for f in fontes]:
            fut.result()


def _abrir_fontes(fontes: list[FonteLeitura], cfg: dict, espera_lock: float,
                  perfil: str = PERFIL_COMPLETO) -> None:
    """Prepara todas as fontes para o laço, em duas fases separadas pelo lock.

    A ordem importa e é a razão de existirem duas fases: só depois de sondar o pipeline se
    sabe QUAIS fontes vão abrir conexão RTSP própria, e o lock dessas tem de ser adquirido
    ANTES da abertura. Fontes servidas pelo pipeline não abrem conexão e não tomam lock —
    tomá-lo faria um bico prender a câmera por toda a leitura (até 28s) e travar o coletor
    de dataset e o snapshot do editor de áreas sem necessidade.
    """
    _em_paralelo(fontes, _sondar_pipeline, cfg)

    # No perfil rápido, fonte cujo pipeline foi sondado e não entregou frame sai da leitura
    # em vez de cair na conexão direta. Essa conexão não é um plano B: o pipeline está com
    # a única conexão que a Intelbras aceita, então a tentativa só pode falhar — e falha
    # depois de gastar o timeout de rede inteiro, que num orçamento de 5s é a chamada
    # toda. No perfil completo ela continua valendo: lá o tempo existe, e o pipeline pode
    # de fato ter morrido e soltado a câmera.
    if perfil == PERFIL_RAPIDO:
        for f in fontes:
            if f.pipeline_sondado and not f.usar_pipeline:
                f.ativa = False
                f.erro = "pipeline sem frame novo dentro do orçamento do modo rápido"
                log.info("%s: fora desta leitura rápida — %s", f.rotulo, f.erro)

    for f in fontes:
        if f.ativa and not f.usar_pipeline:
            f.lock = _obter_lock_camera(f.camera_id)
    _adquirir_locks(fontes, espera_seg=espera_lock)

    _em_paralelo([f for f in fontes if f.ativa and not f.usar_pipeline], _abrir_uma, cfg)


def _liberar_fontes(fontes: list[FonteLeitura]) -> None:
    """Fecha conexões e solta locks de TODAS as fontes, inclusive as abandonadas.

    Percorre por `lock_adquirido`, não por `ativa`: uma fonte descartada pela regra
    adaptativa continua segurando a conexão RTSP e o lock até o fim da leitura, e é
    exatamente ela que ficaria vazando se o critério fosse "ainda está ativa".
    """
    for f in fontes:
        if f.camera_direta is not None:
            try:
                # Não `fechar()` direto: quando a leitora não morre, o cap NÃO pode ser
                # liberado, e o `= None` logo abaixo entregaria o objeto ao coletor de
                # lixo — cujo destrutor faz exatamente o release() proibido, derrubando
                # o processo. `fechar_ou_adiar` segura a referência até ser seguro.
                camera_mod.fechar_ou_adiar(f.camera_direta, f"ler-placa {f.rotulo}")
            except Exception as e:
                log.warning("%s: falha ao fechar câmera: %s", f.rotulo, e)
            f.camera_direta = None
        if f.lock is not None and f.lock_adquirido:
            f.lock.release()
            f.lock_adquirido = False


def _revisar_fontes(fontes: list[FonteLeitura], rodada: int) -> None:
    """Abandona a câmera que não está enxergando placa nenhuma, liberando o orçamento.

    É o que transforma a segunda câmera em ganho líquido. Sem isto, um bico de duas
    câmeras divide o tempo meio a meio — e no caso que motivou a feature (traseira
    bloqueada pelo estepe) metade do orçamento iria para uma câmera que nunca devolveria
    nada, deixando a leitura PIOR que com uma câmera só.

    "Produtiva" é `bboxes > 0`, não `candidatos > 0`: o sinal certo para decidir onde
    gastar tempo é "o enquadramento contém uma placa", que é o que o detector responde.
    Um recorte que o OCR recusou ainda indica que a placa está ali e que vale insistir —
    é resolução/nitidez, problema diferente de enquadramento.

    Função pura sobre a lista de propósito: é a regra que decide a leitura inteira e dá
    para testá-la sem câmera, sem modelo e sem frame.
    """
    if rodada < RODADAS_MINIMAS:
        return
    ativas = [f for f in fontes if f.ativa]
    if len(ativas) < 2:
        return                                   # nunca abandona a última fonte
    if not any(f.bboxes > 0 for f in ativas):
        return                                   # todas em zero: não há como discriminar
    for f in ativas:
        if f.bboxes == 0:
            f.ativa = False
            f.motivo_inativa = (f"sem detecção em {rodada} rodadas "
                                f"({f.tentativas} foto(s))")
            log.info("%s: abandonada — %s", f.rotulo, f.motivo_inativa)


def ler_placa(**kw) -> dict:
    """Rotula com a câmera de origem tudo que a leitura logar, inclusive o que sai do
    fundo do OCR — sem isso as linhas da leitura reativa entram sem dono no mesmo arquivo
    dos pipelines contínuos. Ver app/visao/contexto_log.py. Envelope fino de propósito:
    o corpo é keyword-only, então não há assinatura para duplicar aqui."""
    fontes = kw.get("fontes") or []
    rotulo = "+".join(str(f.camera_id) for f in fontes) or None
    with contexto_log.usar(camera=rotulo):
        return _ler_placa(**kw)


def _ler_placa(
    *,
    fontes: list[FonteLeitura],
    cfg: dict,
    preview_nome: str,
    bico_id: int | None = None,
    origem: str = "roteador",
    avisos: list[str] | None = None,
    perfil: str = PERFIL_COMPLETO,
    # Posto de onde vem a leitura. Existe SÓ para o modo feira saber se pode mockar esta
    # chamada (`app/visao/feira.casar`) — nada mais no laço olha para ele. Opcional porque
    # o bico já identifica a leitura para todo o resto; ausente, o mock não dispara, que é
    # a falha segura.
    empresa_id: int | None = None,
) -> dict:
    """Loop de leitura por confiança ("reject-retry", padrão de mercado ALPR): tira fotos
    incrementalmente e para assim que o consenso entre as leituras ficar forte o bastante
    (ou ao atingir o máximo de tentativas/timeout) — em vez de um número fixo de fotos.

    `fontes` são as câmeras do bico (1 ou 2). Elas se REVEZAM no mesmo laço e alimentam um
    pool único de candidatos, dentro de um único orçamento de tempo — ver o comentário de
    `FonteLeitura`. Cada fonte traz seu próprio ROI e decide sozinha entre reusar o frame
    do pipeline contínuo (quando há um ativo naquela câmera) ou abrir conexão RTSP direta.

    `avisos` traz o que já se sabia antes de começar (ex.: uma das câmeras desativada no
    cadastro); o que falhar durante a abertura é acrescentado aqui dentro.

    `perfil` escolhe entre acurácia e tempo de resposta — ver `PERFIL_RAPIDO`. Ele troca
    DUAS coisas de uma vez, e as duas juntas é que dão o ganho: os modelos (leves, os
    mesmos do stream ao vivo) e o orçamento do laço (uma foto, poucos segundos). O resto
    do corpo desta função não sabe qual perfil está rodando — a eleição, o consenso e o
    limiar de `confirmada` são idênticos nos dois, de propósito: o modo rápido lê menos, e
    isso tem de aparecer como leitura não confirmada, nunca como um limiar mais frouxo.
    """
    if not fontes:
        raise LeituraError(503, "Nenhuma câmera configurada para este bico.")

    obter_detector, det_lock, obter_ocr, ocr_lock = _componentes_do_perfil(perfil)

    avisos = list(avisos or [])
    n_min = max(1, int(_cfg_perfil(cfg, perfil, "snapshots_votacao", "3")))
    n_max = max(n_min, int(_cfg_perfil(cfg, perfil, "leitura_max_tentativas", "12")))
    timeout_seg = float(_cfg_perfil(cfg, perfil, "leitura_timeout_seg", "6"))
    acordo_min = float(cfg.get("leitura_acordo_minimo", "0.80"))
    # Lido UMA vez por leitura: a parada antecipada e a decisao final tem de usar a MESMA
    # metrica, senao o loop para com um numero e o banco grava outro.
    metrica_acordo = _acordo_metrica(cfg)
    # Instrumentacao, nao comportamento: com ela ligada o laco ELEGE a partir da 2a foto e
    # registra o que teria emitido ali, mas continua parando pelo piso real (`n_min`).
    log_parcial = str(cfg.get("leitura_log_parcial", "nao")).strip().lower() in (
        "sim", "true", "1")

    def _leituras_do_continuo() -> list[tuple[str, float]]:
        """Leituras que o pipeline continuo ja fez nas cameras deste bico.

        Recolhido a cada eleicao, e nao uma vez antes do laco: o continuo segue rodando
        durante os 28 s da chamada, e uma foto boa que ele tire no meio do caminho tem de
        poder entrar na votacao.
        """
        extras: list[tuple[str, float]] = []
        for f in fontes:
            if f.leituras_provider is None:
                continue
            try:
                extras.extend(f.leituras_provider() or [])
            except Exception as e:
                log.debug("%s: leituras do continuo indisponiveis (%s)", f.rotulo, e)
        return extras

    # Cobre a chamada INTEIRA, inclusive esperas antes do laço (pipeline_frame_provider,
    # lock de câmera, conexão RTSP) — é a referência para o orçamento de tempo real que o
    # roteador sente. Ajustada mais abaixo para excluir só a carga de modelo (linha ~344),
    # não essas esperas: diferente da carga (custo único pós-boot), elas são latência real
    # de cada chamada e precisam contar contra `timeout_seg`, senão o total de ponta a
    # ponta pode passar bem além do configurado sem o laço perceber.
    inicio_absoluto = time.time()

    _abrir_fontes(fontes, cfg, espera_lock=min(5.0, timeout_seg / 4), perfil=perfil)

    for f in fontes:
        if f.erro:
            avisos.append(f"{f.rotulo}: {f.erro}")
    ativas_iniciais = [f for f in fontes if f.ativa]
    if not ativas_iniciais:
        # Só agora é erro de verdade: com duas câmeras, perder uma é degradação — perder
        # as duas é que deixa a leitura sem nenhuma imagem para analisar.
        detalhe = "; ".join(f"{f.rotulo}: {f.erro}" for f in fontes if f.erro)
        _liberar_fontes(fontes)
        raise LeituraError(503, f"Nenhuma câmera do bico respondeu — {detalhe}")
    if avisos:
        log.warning("ler-placa bico_id=%s: seguindo degradado — %s", bico_id, "; ".join(avisos))

    candidatos: list[dict] = []
    # Quantos recortes o DETECTOR entregou ao longo do loop, mesmo os que o OCR recusou.
    # Sem isso, "detector não viu placa nenhuma" e "viu, mas o OCR não leu" chegavam ao
    # usuário como a mesma mensagem — e são problemas opostos (enquadramento/modelo de
    # detecção vs resolução/nitidez da placa), que se resolvem de formas diferentes.
    bboxes_total = 0
    # `frame_principal`/`nitidez_principal` viviam aqui e migraram para `FonteLeitura`
    # (cada câmera tem o seu preview). As locais ficaram para trás: `nitidez_principal`
    # nunca mais foi lida, e `frame_principal` é reatribuída sem condição antes do primeiro
    # uso, a partir da fonte mais nítida.
    tentativas = 0
    parada_motivo = "max_tentativas"
    inicio = inicio_absoluto

    try:
        # ── Detector e OCR ────────────────────────────────────────────────────
        # No perfil completo, componentes de ALTA PRECISÃO independentes do stream ao vivo:
        # detecção 2 estágios veículo→placa + varredura em janelas + OCR com reforço
        # PaddleOCR. Ambos toleram a latência maior do fluxo sob demanda.
        #
        # No perfil rápido, exatamente os componentes do stream ao vivo — que é o ponto de
        # operação já conhecido em produção para "ler em tempo real". Ver
        # `_componentes_do_perfil`.
        t_antes_modelo = time.time()
        det_inst = obter_detector(cfg)
        ocr_inst = obter_ocr(cfg)
        tempo_carga_modelo = time.time() - t_antes_modelo

        # Desloca a referência do orçamento só pelo tempo de CARGA DE MODELO: na primeira
        # leitura após subir o servidor, isso leva dezenas de segundos e não deve consumir
        # o orçamento (custo único, não repete nas próximas chamadas). A espera anterior
        # (pipeline/lock/conexão) continua contando — ver comentário em `inicio_absoluto`.
        inicio = inicio_absoluto + tempo_carga_modelo

        # ── Loop de leitura: as fontes se revezam até o consenso ficar forte ────
        # `tentativas`, `n_max` e `n_min` são GLOBAIS (soma das fontes), não por câmera:
        # `tentativas` é o tamanho do pool de evidência que `_eleger_placa` vota e o
        # denominador de `total_snapshots` no contrato. Torná-los por fonte daria 24 fotos
        # a um bico de duas câmeras e estouraria a tolerância de ~25-30s do roteador de um
        # jeito invisível no código.
        cursor = 0
        rodada = 0
        while tentativas < n_max:
            if time.time() - inicio > timeout_seg:
                parada_motivo = "timeout"
                break

            ativas = [f for f in fontes if f.ativa]
            if not ativas:
                break
            # Só na virada da rodada é que a lista de ativas é reavaliada — reavaliar a
            # cada turno mudaria os índices no meio da volta e faria uma fonte perder a vez.
            if cursor >= len(ativas):
                cursor = 0
                rodada += 1
                _revisar_fontes(fontes, rodada)
                ativas = [f for f in fontes if f.ativa]
                if not ativas:
                    break
            f = ativas[cursor]
            cursor += 1

            # Cadência POR FONTE, não por tentativa: o sleep existe para dar tempo de
            # ESTA câmera publicar um frame novo. Global, com duas fontes cada câmera
            # seria revisitada com o dobro do intervalo e o total de fotos cairia à toa.
            # Com uma fonte só, é exatamente o comportamento de sempre.
            intervalo = 0.15 if f.usar_pipeline else 0.5
            if f.tentativas:
                espera = intervalo - (time.time() - f.ultimo_ts)
                if espera > 0:
                    time.sleep(espera)

            if f.tentativas == 0 and f.frame_inicial is not None:
                frame = f.frame_inicial
            elif f.usar_pipeline:
                frame = f.provider()
            else:
                frame = f.camera_direta.ler() if f.camera_direta is not None else None
                if frame is not None and f.ajustador is not None and f.ajustador.ativo:
                    frame = f.ajustador.processar(frame)

            f.ultimo_ts = time.time()
            if frame is None:
                # Frame ausente não conta como tentativa nem como voto (o provider do
                # pipeline devolve None quando ainda não há frame NOVO). Com várias fontes
                # não dá para dormir aqui: isso pararia o laço inteiro por causa de uma.
                if len(ativas) == 1:
                    time.sleep(0.1)
                continue
            tentativas += 1
            f.tentativas += 1

            # Melhor frame p/ preview = o mais nítido entre os capturados (Laplaciano).
            # Por FONTE, porque cada câmera tem o seu próprio preview a apresentar.
            cinza = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            nitidez = cv2.Laplacian(cinza, cv2.CV_64F).var()
            if nitidez > f.nitidez_principal:
                f.nitidez_principal = nitidez
                f.frame_principal = frame

            # Locks: det_inst/ocr_inst são instâncias CACHEADAS compartilhadas entre
            # requests concorrentes (2+ bicos podem "Ler Placa" ao mesmo tempo). Em
            # CUDAExecutionProvider (GPU), chamadas concorrentes na mesma sessão onnxruntime
            # podem travar/crashar — o lock serializa só a chamada individual, não o loop
            # inteiro, pra não bloquear um bico pela duração toda da leitura do outro.
            with contexto_log.usar(camera=f.camera_id):
                bboxes = _detectar(det_inst, frame, f.roi, det_lock)
                bboxes_total += len(bboxes)
                f.bboxes += len(bboxes)
                f_h, f_w = frame.shape[:2]
                for bb in bboxes:
                    x, y, w, h, conf_det = bb
                    # Antes de `_expandir_bbox`, que devolve tupla crua e perderia a origem.
                    origem_tipo = origem_de_bbox(bb)
                    x, y, w, h = _expandir_bbox(x, y, w, h, f_w, f_h)
                    crop = frame[y: y + h, x: x + w]
                    if crop.size == 0:
                        continue

                    if hasattr(ocr_inst, "ler_detalhado"):
                        with ocr_lock:
                            ocr_res = ocr_inst.ler_detalhado(crop)
                        if not ocr_res["placa"]:
                            continue
                        placa      = ocr_res["placa"]
                        padrao     = ocr_res["padrao"]
                        conf_ocr   = ocr_res["confianca"]
                        votos_ocr  = ocr_res["votos"]
                        total_eng  = ocr_res["total_engines"]
                        det_ocr    = ocr_res["detalhes"]
                    else:
                        with ocr_lock:
                            texto, conf_ocr = ocr_inst.ler(crop)
                        resultado = validar(texto)
                        if not resultado:
                            continue
                        placa, padrao = resultado
                        votos_ocr = 1
                        total_eng = 1
                        det_ocr   = [{"engine": getattr(ocr_inst, "engine", "?"), "placa": placa,
                                       "padrao": padrao, "confianca": round(conf_ocr, 3)}]

                    f.candidatos += 1
                    candidatos.append({
                        "placa":         placa,
                        "padrao":        padrao,
                        "confianca":     round((conf_det + conf_ocr) / 2, 3),
                        "votos_ocr":     votos_ocr,
                        "total_engines": total_eng,
                        "detalhes_ocr":  det_ocr,
                        "crop":          crop,
                        "bbox":          {"x": x, "y": y, "w": w, "h": h},
                        "frame":         frame,
                        # De QUAL câmera este voto veio. Sem isto não dá para gravar em
                        # `deteccoes.camera_db_id` o ângulo que de fato leu a placa, e o
                        # histórico atribuiria a leitura à câmera errada — que é o que a
                        # próxima chamada usa para cruzar com o pipeline.
                        "camera_db_id":  f.camera_id,
                        "papel":         f.papel,
                        # Origem do tipo de veículo (classe do YOLOX + sinal cru) desta
                        # bbox. `tipo_veiculo` é None quando o 2 estágios não rodou, não
                        # achou veículo, ou a placa veio da varredura em janelas — nunca um
                        # chute. Vem por candidato e não de um atributo lido depois do laço
                        # porque `_eleger_placa` pode eleger um candidato que não é o
                        # último analisado, e aí o tipo gravado seria o de outro recorte (e
                        # possivelmente outro veículo). Os quatro campos vêm juntos da
                        # mesma `OrigemTipo` — nunca gravar um sem os outros três.
                        "tipo_veiculo":        origem_tipo.tipo,
                        "veiculo_classe":      origem_tipo.classe,
                        "veiculo_conf":        origem_tipo.conf,
                        "tipo_veiculo_fonte":  origem_tipo.fonte,
                    })

            # Parada antecipada: só depois do mínimo de fotos, e só se o consenso for forte
            # o bastante (evita parar num acerto isolado de sorte na 1ª foto).
            #
            # `n_votos_snap >= 2` é essencial aqui, não redundante com `acordo >= acordo_min`:
            # o pool de "leituras" do _eleger_placa mistura a placa de cada candidato COM
            # cada engine individual dele (linha ~131). Para carro com boa confiança, AutoOCR
            # nem roda o engine de fallback (só 1 entrada em detalhes_ocr) — se só 1 dos
            # frames capturados até agora produziu detecção válida, esse único candidato
            # entra 2x no pool e "acordo" fecha em 1.0 sozinho, sem nenhuma concordância
            # ENTRE frames de verdade. Sem essa segunda checagem, o loop parava ali,
            # abrindo mão do resto do orçamento de tempo/tentativas que poderia confirmar
            # (ou contradizer) essa única leitura.
            # `PISO_CONTRAFACTUAL` (2) e nao `n_min`: com `leitura_log_parcial` ligado a
            # eleicao passa a rodar a partir da 2a foto, para o log poder responder "com
            # `snapshots_votacao=2`, esta chamada teria parado aqui, e com que placa?".
            # Sem isso a pergunta so e respondivel trocando o valor em producao e
            # observando o resultado — que e a aposta que este log existe para evitar.
            #
            # A eleicao extra e segura: `_eleger_placa` e pura (devolve `dict(max(...))`,
            # nao muta `candidatos` nem estado global). `_leituras_do_continuo()` NAO e —
            # ela consulta o tracker do pipeline — por isso e chamada UMA vez e o
            # resultado reaproveitado, nunca duas vezes na mesma iteracao.
            piso = min(n_min, PISO_CONTRAFACTUAL) if log_parcial else n_min
            if tentativas >= piso and candidatos:
                eleito_parcial = _eleger_placa(candidatos, metrica_acordo,
                                               _leituras_do_continuo(),
                                               com_alternativa=log_parcial)
                # A MESMA contagem que decide `confirmada` no fim. Se a parada usasse fotos
                # e a confirmacao usasse leituras, o laco correria ate o timeout mesmo com
                # evidencia suficiente - e `web/leitura.py::_status` rebaixa por timeout,
                # anulando o ganho. Os dois gates tem de olhar o mesmo numero.
                # A MESMA regra da confirmacao final (`_confirmada` logo abaixo do laco),
                # incluindo a tranca de FOTOS. Sem ela o laco parava apoiado em leituras
                # que vinham todas da mesma foto: medido em 01/09/2026, a chamada
                # `QFB3107` fechava na foto 2 com `votos_snap=1, cands=1` — 4 leituras de
                # engine sobre UM recorte, que concordam sobre o falso positivo se houver
                # um. As outras 3 do mesmo dia tinham 2 fotos e seguem passando.
                #
                # Parada e confirmacao TEM de olhar o mesmo numero: se a parada fosse mais
                # frouxa, o laco pararia cedo e o resultado sairia nao-confirmado assim
                # mesmo — gastando o orcamento sem entregar o selo.
                fecha = bool(eleito_parcial
                             and _confirmada(eleito_parcial["acordo"],
                                             eleito_parcial["n_votos_leitura"],
                                             acordo_min, n_min,
                                             n_fotos=eleito_parcial["n_votos_snap"]))
                if log_parcial and eleito_parcial:
                    # INFO e nao DEBUG de proposito: com `log_level=debug` cada modelo do
                    # ensemble emite uma linha por recorte e esta se perderia no meio. Sob
                    # flag propria (default "nao"), uma linha por foto e greppavel e
                    # sobrevive a producao com `log_level=info`.
                    #
                    # `placa` aqui e a ELEITA nesta foto, ainda ANTES de
                    # `_mesclar_com_historico` (que roda depois do laco e pode trocar a placa
                    # ao fundir com uma leitura recente do mesmo bico). Isso e o certo para a
                    # pergunta: as duas alternativas — parar na 2a ou na 3a — passariam pelo
                    # MESMO merge, entao comparar as eleicoes isola a variavel.
                    #
                    # NAO existe coluna `confirmaria`: ela seria
                    # `confirmada(acordo, votos, acordo_min, 2)`, que expande para
                    # `acordo >= acordo_min and votos >= 2` — exatamente `pararia`. Medido em
                    # 20 combinacoes de (acordo, votos): identicas em todas. Uma coluna
                    # sempre igual a outra nao informa, e sugere que informa.
                    # `acordo` e o da metrica CONFIGURADA (a que decide `pararia`);
                    # `acordo_alt` e a outra escala sobre o mesmo pool. Ter as duas lado a
                    # lado responde, na mesma campanha, se trocar `acordo_metrica`
                    # resolveria os casos de leitura certa que nao fecha o acordo.
                    log.info(
                        "leitura-parcial bico=%s foto=%d/%s placa=%s padrao=%s "
                        "metrica=%s acordo=%.3f acordo_alt=%.3f "
                        "votos_leitura=%d votos_snap=%d cands=%d pararia=%s t_ms=%d",
                        bico_id, tentativas, n_min, eleito_parcial["placa"],
                        eleito_parcial["padrao"], metrica_acordo,
                        eleito_parcial["acordo"], eleito_parcial.get("acordo_alt", -1.0),
                        eleito_parcial["n_votos_leitura"], eleito_parcial["n_votos_snap"],
                        len(candidatos), fecha,
                        int((time.time() - inicio_absoluto) * 1000))
                # O piso REAL continua sendo `n_min` — o log nao pode mudar quando o laco
                # para, senao ele mede a si mesmo em vez de medir a producao.
                if tentativas >= n_min and fecha:
                    parada_motivo = "acordo"
                    break
    finally:
        _liberar_fontes(fontes)

    # Melhor frame entre TODAS as fontes — só para o caso sem placa, onde não há candidato
    # eleito que aponte um quadro. Guarda também a FONTE dele: o ROI a desenhar é o
    # daquela câmera, e não o da primeira — pintar o retângulo da traseira no quadro da
    # frente enganaria exatamente quem está olhando o preview para achar o erro de
    # enquadramento.
    com_frame = [f for f in fontes if f.frame_principal is not None]
    fonte_nitida = max(com_frame, key=lambda f: f.nitidez_principal) if com_frame else None
    frame_principal = fonte_nitida.frame_principal if fonte_nitida is not None else None

    if frame_principal is None:
        # Distingue "a câmera não entregou imagem" de "o tempo acabou antes de tentar" —
        # antes as duas situações davam a mesma mensagem, culpando a câmera à toa.
        if parada_motivo == "timeout" and tentativas == 0:
            raise LeituraError(
                503,
                f"Tempo esgotado ({timeout_seg:.0f}s) antes de analisar qualquer imagem. "
                "Pode ter sido demora para conectar à câmera/pipeline, ou (se foi logo após "
                "reiniciar o servidor) carga inicial de modelo — tente de novo. Caso "
                "persista, aumente `leitura_timeout_seg`.",
            )
        raise LeituraError(503, "Câmera conectou mas não enviou frames — verifique a conexão")

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    leituras_continuo = _leituras_do_continuo()
    melhor = (_eleger_placa(candidatos, metrica_acordo, leituras_continuo)
              if candidatos else None)

    # Câmera do candidato ELEITO — é ela que vai para o histórico. `.get` com fallback
    # porque `_mesclar_com_anterior` remonta o dict a partir da leitura anterior e pode
    # não trazer a chave; gravar None quebraria o cruzamento com o pipeline na chamada
    # seguinte.
    # `is None` e não `or`: `or` trata o id 0 como ausente. Os ids vêm de AUTOINCREMENT
    # (começam em 1), mas `camera_db_id: int = 0` é o default de `Pipeline` e de
    # `AjustadorAmbiente` — um dublê ou um caminho de teste com id 0 atribuiria a leitura à
    # fonte errada em silêncio. (Auditoria 27/08/2026.)
    _eleita = (melhor or {}).get("camera_db_id")
    camera_eleita = _eleita if _eleita is not None else fontes[0].camera_id
    fonte_eleita = next((f for f in fontes if f.camera_id == camera_eleita), fontes[0])

    anterior_id: int | None = None
    if melhor is not None and bico_id is not None:
        melhor, anterior_id = _mesclar_com_historico(
            melhor, bico_id=bico_id, camera_ids=[f.camera_id for f in fontes], origem=origem,
            cooldown_seg=float(cfg.get("cooldown_seg", "120")),
        )

    def _desenhar(frame, roi_fonte, bbox=None, placa=None):
        """Quadro anotado: caixa do OCR (quando a placa saiu deste frame) + área do bico."""
        img = frame.copy()
        if bbox is not None:
            cv2.rectangle(img, (bbox["x"], bbox["y"]),
                          (bbox["x"] + bbox["w"], bbox["y"] + bbox["h"]), (0, 200, 255), 2)
            cv2.putText(img, placa, (bbox["x"], max(bbox["y"] - 8, 14)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 255), 2)
        # Marca a área do bico, para conferir o enquadramento junto com o resultado
        if roi_fonte:
            cv2.rectangle(img, (roi_fonte["x"], roi_fonte["y"]),
                          (roi_fonte["x"] + roi_fonte["w"], roi_fonte["y"] + roi_fonte["h"]),
                          (120, 120, 120), 1)
        return img

    # Preview canônico: quando houve leitura, mostra o FRAME DE ONDE a placa vencedora
    # saiu, com a caixa exata que o OCR usou. Antes rodava uma segunda detecção sobre o
    # frame mais nítido — custava uma passada inteira do detector e podia desenhar caixa
    # diferente (ou nenhuma) da que foi realmente lida, atrapalhando auditar um erro.
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    if melhor is not None:
        frame_preview = _desenhar(melhor["frame"], fonte_eleita.roi,
                                  melhor["bbox"], melhor["placa"])
    else:
        frame_preview = _desenhar(frame_principal, fonte_nitida.roi)
    cv2.imwrite(str(PREVIEW_DIR / f"{preview_nome}.jpg"), frame_preview,
                [int(cv2.IMWRITE_JPEG_QUALITY), 80])

    # Preview POR CÂMERA: com duas, o operador precisa conferir o enquadramento das duas
    # de uma vez — inclusive (principalmente) o da que não achou nada, que é onde está o
    # problema a corrigir. Só é gravado quando há mais de uma fonte: com uma, o canônico
    # acima já é esse quadro e um segundo arquivo idêntico seria só lixo em disco.
    if len(fontes) > 1:
        for f in fontes:
            if f.frame_principal is None:
                # APAGA o preview desta camera em vez de deixar o arquivo anterior.
                # O `continue` sozinho preservava no disco o quadro da leitura PASSADA, e a
                # rota `/api/bicos/{id}/preview.jpg` o servia sem saber que era velho: a tela
                # mostrava "traseira - 0 foto(s), 0 deteccao(oes)" ao lado da imagem de OUTRO
                # dia, com a moto de ontem no quadro. Quem olha aquilo conclui que a camera
                # viu a moto e nao leu, quando a camera nao entregou frame nenhum -- o
                # oposto do diagnostico. Ausente, a rota devolve 404 e a tela nao mostra
                # imagem, que e a verdade.
                caminho_antigo = caminho_preview_bico(bico_id, f.camera_id)
                try:
                    caminho_antigo.unlink(missing_ok=True)
                except OSError as e:
                    log.warning("%s: nao consegui apagar preview velho %s (%s)",
                                f.rotulo, caminho_antigo, e)
                continue
            usa_bbox = melhor is not None and f.camera_id == camera_eleita
            img = _desenhar(melhor["frame"] if usa_bbox else f.frame_principal, f.roi,
                            melhor["bbox"] if usa_bbox else None,
                            melhor["placa"] if usa_bbox else None)
            cv2.imwrite(str(caminho_preview_bico(bico_id, f.camera_id)), img,
                        [int(cv2.IMWRITE_JPEG_QUALITY), 80])

    # Rota autenticada, não URL estática direta — ver o comentário de `PREVIEW_DIR`.
    # `bico_id` é None só em teoria (os dois chamadores de `ler_placa` sempre o passam,
    # sincronizado com `preview_nome`); sem ele não há como montar a rota, então o
    # preview some do payload em vez de apontar para um caminho que não existe mais.
    frame_url = f"/api/bicos/{bico_id}/preview.jpg" if bico_id is not None else None

    def _fontes_para_log() -> str:
        """`cam3 (frente) 2f/2bb/2c` por fonte — a MESMA string nos dois desfechos.

        Existia inline só no ramo de sucesso. Repetir a expressão no desfecho sem placa
        deixaria as duas livres para divergir, e é exatamente comparando as duas linhas —
        uma leitura que deu e uma que não deu, na mesma câmera — que se separa problema de
        detecção de problema de OCR.
        """
        return ", ".join(f"{f.rotulo} {f.tentativas}f/{f.bboxes}bb/{f.candidatos}c"
                         f"{'' if f.ativa else ' ABANDONADA'}" for f in fontes)

    def _resumo_fontes() -> list[dict]:
        """O que cada câmera contribuiu — é o diagnóstico que a tela do posto e o editor
        de áreas mostram, e o único jeito de medir em campo se a segunda câmera vale."""
        return [{
            "camera_id": f.camera_id, "papel": f.papel, "estado": f.estado(),
            "motivo": f.erro or f.motivo_inativa or "",
            "tentativas": f.tentativas, "bboxes": f.bboxes, "candidatos": f.candidatos,
            # `None` quando ESTA camera nao entregou quadro nesta chamada. Sem isso o
            # payload prometia uma imagem que nao existe (ou pior, a da leitura anterior),
            # e a tela renderizava um <img> apontando para 404 ou para o quadro de ontem.
            "frame_url": (None if f.frame_principal is None else
                          f"/api/bicos/{bico_id}/preview.jpg?camera_id={f.camera_id}"
                          if bico_id is not None and len(fontes) > 1 else frame_url),
        } for f in fontes]

    # `melhor` só é None quando `candidatos` está vazio (_eleger_placa sempre elege algo a
    # partir de um candidato), então os dois casos de falha se separam por `bboxes_total`:
    # o detector não achou placa, ou achou e nenhum recorte virou texto válido.
    if not candidatos:
        if bboxes_total:
            mensagem = (f"Placa localizada em {bboxes_total} recorte(s), mas o texto não foi "
                        "reconhecido — placa pequena, borrada ou muito inclinada para o OCR")
        else:
            mensagem = ("Nenhuma placa detectada nos frames — verifique o enquadramento da "
                        "área do bico e se o veículo aparece dentro dela")
        # O desfecho SEM placa também vai para o log, pela mesma razão do ramo de sucesso.
        # Sem esta linha ele não deixava rastro NENHUM: o `leitura-parcial` do laço só
        # dispara quando há candidato, e este return era mudo. Medido em 04/09/2026: as
        # ~40 chamadas de `/api/feira/scan` de uma demonstração devolveram 200 sem placa e
        # o log não tinha uma única linha sobre nenhuma delas — a câmera era uma virtual
        # camera do OBS servindo o quadro "sem fonte de vídeo", e descobrir isso exigiu
        # abrir na mão os JPEGs que a coleta de dataset tinha salvo por acaso.
        #
        # `bboxes` é o campo que separa os dois diagnósticos, e eles pedem investigações
        # OPOSTAS: 0 = o detector não achou placa (enquadramento, câmera, cena vazia);
        # >0 = achou e nenhum recorte virou texto válido (placa pequena, borrada, inclinada).
        # É a mesma distinção que a `mensagem` logo acima faz para o humano na tela.
        log.info("Ler-placa[%s]: SEM PLACA (bboxes=%d, tentativas=%d/%d, parada=%s, "
                 "camera_id=%d, bico_id=%s, fontes=[%s])",
                 perfil, bboxes_total, tentativas, n_max, parada_motivo,
                 fonte_nitida.camera_id, bico_id, _fontes_para_log())
        # A câmera do quadro que `frame_url` mostra — reportar outra faria o painel
        # atribuir o preview à câmera errada justamente no caso em que alguém está
        # olhando para descobrir qual das duas está mal enquadrada.
        # `mockada` sai nos DOIS desfechos, pelo mesmo motivo do `modo`: um campo que só
        # aparece quando se leu placa obriga o consumidor a tratar ausência como um
        # terceiro estado. Aqui é sempre `False` — e não "não sei": este `return` acontece
        # ANTES do gancho do modo feira, e sem string do OCR o mock nem roda (não há o que
        # casar por distância de edição).
        return {"placa": None, "mensagem": mensagem, "frame_url": frame_url,
                "camera_id": fonte_nitida.camera_id, "bico_id": bico_id, "mockada": False,
                "bboxes_detectadas": bboxes_total, "fontes": _resumo_fontes(), "avisos": avisos,
                "snapshots_analisados": tentativas, "tentativas": tentativas,
                "parada_motivo": parada_motivo, "modo": perfil}

    n_votos_snap = melhor.pop("n_votos_snap")
    # `.pop` com default: `_mesclar_com_anterior`/`_mesclar_com_historico` remontam o dict a
    # partir da leitura anterior e podem nao trazer a chave nova.
    n_votos_leitura = melhor.pop("n_votos_leitura", n_votos_snap)
    acordo_final = melhor.pop("acordo")

    # LEITURAS, e nao fotos. `consenso.confirmada` NAO muda de assinatura de proposito: ela
    # tem outros dois chamadores em `pipeline.py`, e no continuo a unidade ja esta certa -
    # cada voto e uma passada de OCR em frame DIFERENTE, entao ha varias leituras
    # independentes de verdade. Aqui, com o GET conseguindo 1 foto em 28 s, "2 fotos" era
    # inalcancavel e nada era confirmado; "2 leituras" e o que o ensemble de fato produz.
    confirmada = _confirmada(acordo_final, n_votos_leitura, acordo_min, n_min,
                             n_fotos=n_votos_snap)

    # ── Modo feira (MOCK) ─────────────────────────────────────────────────────
    # Roda AQUI, depois da eleicao/fusao e do veredito, e antes de gravar. E esse ponto
    # que faz o mock "prevalecer": ele sobrepoe ate uma leitura real confiante que errou
    # um caractere, sem ter encostado em detector, OCR, consenso ou merge — se o modo
    # estiver desligado (o padrao), nada aqui executa.
    #
    # As tres marcacoes abaixo NAO sao opcionais. Leitura mockada e dado sintetico, e
    # dado sintetico ja inverteu o sinal de uma medicao neste projeto: sem `origem="feira"`
    # ela entraria na taxa de acerto, no painel de integracao e na fila do /testes como se
    # fosse leitura de verdade.
    placa_demo = feira_mod.casar(melhor["placa"], cfg, empresa_id)
    if placa_demo is not None:
        log.warning("MODO FEIRA: leitura '%s' substituida pela placa de demonstracao '%s' "
                    "(bico=%s). Leitura MOCK, gravada com origem='feira'.",
                    melhor["placa"], placa_demo, bico_id)
        _v_demo = validar(placa_demo)
        melhor["placa"] = placa_demo
        # `padrao` recalculado junto com a placa: deixar o padrao da leitura ANTIGA faria
        # o historico mostrar uma Mercosul rotulada como antiga (ou o contrario).
        if _v_demo:
            melhor["placa"], melhor["padrao"] = _v_demo
        melhor["confianca"] = 1.0
        acordo_final = 1.0
        confirmada = True
        origem = feira_mod.ORIGEM
        avisos.append(
            f"modo feira: placa de demonstracao '{melhor['placa']}' reconhecida (MOCK) — "
            "esta leitura NAO veio do OCR")

    # Tipo estimado do candidato ELEITO, e o sinal cru por trás dele (`_eleger_placa`
    # devolve uma cópia do candidato, então as chaves vêm juntas). `.get` e não indexação:
    # `_mesclar_com_anterior`/`_mesclar_com_historico` remontam o dict a partir da leitura
    # anterior e podem não trazer as chaves. Os quatro vêm sempre juntos — nunca gravar
    # `tipo_veiculo` sem o `veiculo_classe`/`veiculo_conf`/`tipo_veiculo_fonte` que o
    # explicam.
    tipo_veiculo = melhor.get("tipo_veiculo")
    veiculo_classe = melhor.get("veiculo_classe")
    veiculo_conf = melhor.get("veiculo_conf")
    tipo_veiculo_fonte = melhor.get("tipo_veiculo_fonte")

    # ── Quadro inteiro desta detecção ─────────────────────────────────────────
    # O preview acima é sobrescrito a cada leitura; aqui guardamos uma cópia com nome
    # único, para o histórico poder mostrar o contexto (qual veículo, onde estava)
    # e não só o recorte da placa.
    frame_rel = None
    if cfg.get("salvar_frame_deteccao", "sim").lower() in ("sim", "true", "1"):
        ts_f = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        nome_f = f"{ts_f}_{melhor['placa']}_frame.jpg"
        cv2.imwrite(str(SNAPSHOT_DIR / nome_f), frame_preview,
                    [int(cv2.IMWRITE_JPEG_QUALITY), int(cfg.get("snapshot_qualidade", "85"))])
        frame_rel = f"/static/snapshots/{nome_f}"

    # ── Snapshot do crop ──────────────────────────────────────────────────────
    snapshot_rel = None
    if cfg.get("salvar_snapshot", "").lower() in ("sim", "true", "1"):
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        nome = f"{ts}_{melhor['placa']}.jpg"
        cv2.imwrite(str(SNAPSHOT_DIR / nome), melhor["crop"],
                    [int(cv2.IMWRITE_JPEG_QUALITY), int(cfg.get("snapshot_qualidade", "85"))])
        snapshot_rel = f"/static/snapshots/{nome}"

    # ── Persiste ──────────────────────────────────────────────────────────────
    # Se mesclou com a detecção anterior do mesmo bico, ATUALIZA aquela linha em vez de
    # inserir uma nova — o histórico mostra um evento por veículo, não um por chamada.
    if anterior_id is not None:
        _com_retry_lock(lambda: banco.atualizar_deteccao(
            anterior_id, placa=melhor["placa"], padrao=melhor["padrao"],
            confianca=melhor["confianca"], snapshot=snapshot_rel, frame=frame_rel,
            acordo=acordo_final, confirmada=confirmada, tipo_veiculo=tipo_veiculo,
            veiculo_classe=veiculo_classe, veiculo_conf=veiculo_conf,
            tipo_veiculo_fonte=tipo_veiculo_fonte,
            # A câmera da fonte ELEITA nesta chamada — o mesmo valor que o ramo de INSERT
            # abaixo grava, e pelo mesmo motivo que o comentário de lá explica. Sem isto a
            # linha mesclada ficava com a câmera da chamada anterior.
            camera_db_id=camera_eleita,
        ), "atualizar detecção")
        det_id = anterior_id
    else:
        # Câmera da FONTE ELEITA, não da primária: com duas câmeras, gravar a errada faz
        # a próxima chamada cruzar o pipeline pela câmera errada e duplicar o veículo no
        # histórico — além de atribuir a leitura ao ângulo que não a produziu.
        det_id = _com_retry_lock(lambda: banco.registrar_deteccao(
            placa=melhor["placa"], padrao=melhor["padrao"], confianca=melhor["confianca"],
            snapshot=snapshot_rel, camera_id=fonte_eleita.especificacao.camera_tipo,
            bbox=melhor["bbox"],
            bico_id=bico_id, frame=frame_rel, origem=origem, camera_db_id=camera_eleita,
            acordo=acordo_final, confirmada=confirmada, tipo_veiculo=tipo_veiculo,
            veiculo_classe=veiculo_classe, veiculo_conf=veiculo_conf,
            tipo_veiculo_fonte=tipo_veiculo_fonte,
        ), "registrar detecção")
    estado.adicionar_deteccao({
        "id": det_id, "placa": melhor["placa"], "padrao": melhor["padrao"],
        "confianca": melhor["confianca"], "snapshot": snapshot_rel,
        "criado_em": datetime.now(timezone.utc).isoformat(),
        # Sem isto o painel de recentes mostraria uma leitura fraca idêntica a uma
        # sólida, contradizendo o que ficou gravado no banco.
        "acordo": acordo_final, "confirmada": confirmada,
        "tipo_veiculo": tipo_veiculo,
    })
    # Quantas CÂMERAS distintas votaram na placa eleita. Não entra em `_confirmada` de
    # propósito: mudar política de consenso sem amostra medida é o erro que o AutoOCR já
    # documenta ter cometido. Fica exposto para dar como medir, em campo, se dois ângulos
    # concordando valem mais que dois frames do mesmo ângulo — e só depois decidir.
    n_cameras_votando = len({c["camera_db_id"] for c in candidatos
                             if c["placa"] == melhor["placa"] and c.get("camera_db_id")})

    # `modo` no log, e não só no payload: sem ele uma leitura fraca do perfil rápido é
    # indistinguível de uma leitura fraca do completo, e as duas pedem investigações
    # opostas (a primeira é o preço esperado do modo, a segunda é problema de câmera).
    log.info("Ler-placa[%s]: %s (%s, conf=%.2f, acordo=%.2f%s, tipo=%s, tentativas=%d/%d, parada=%s, "
             "ocr=%d/%d, camera_id=%d, bico_id=%s, fontes=[%s])",
             perfil,
             melhor["placa"], melhor["padrao"], melhor["confianca"], acordo_final,
             "" if confirmada else " NAO-CONFIRMADA",
             tipo_veiculo or "nao-estimado",
             tentativas, n_max, parada_motivo, melhor["votos_ocr"], melhor["total_engines"],
             camera_eleita, bico_id, _fontes_para_log())

    return {
        # A câmera de onde saiu a placa eleita — com uma fonte é a de sempre.
        "camera_id":           camera_eleita,
        "bico_id":             bico_id,
        # Esta placa veio do MOCK do modo feira, não do OCR. Sai no payload, e não só no
        # banco, porque até aqui o único sinal disso para quem consome era uma frase em
        # `avisos` — texto livre, que nenhum consumidor tipado lê.
        #
        # É `placa_demo is not None`, e NÃO `origem == "feira"`: a origem também vale
        # "feira" quando é o próprio fluxo da vitrine que está chamando
        # (`POST /api/feira/scan` pede `origem="feira"` para a leitura ficar fora de
        # 'producao'), e nesse caminho a placa do celular de um visitante sairia marcada
        # como mockada sem nunca ter passado por `casar`. Confundir as duas fez o card da
        # vitrine saudar placa de visitante como veículo de demonstração.
        "mockada":             placa_demo is not None,
        "placa":               melhor["placa"],
        "padrao":              melhor["padrao"],
        "confianca":           melhor["confianca"],
        "votos_snapshot":      n_votos_snap,
        # Campo NOVO, ao lado de `votos_snapshot` e nunca no lugar dele: `votos_snapshot` e
        # `total_snapshots` sao contrato publicado (docs/INTEGRACAO_ROTEADOR.md) e continuam
        # significando FOTOS. Redefinir campo que o sidecar Java ja le, em silencio, seria
        # pior que nao corrigir nada.
        "votos_leitura":       n_votos_leitura,
        "total_snapshots":     tentativas,
        "votos_ocr":           melhor["votos_ocr"],
        "total_engines":       melhor["total_engines"],
        "detalhes_ocr":        melhor["detalhes_ocr"],
        "snapshot":            snapshot_rel,
        "frame_url":           frame_url,
        "tentativas":          tentativas,
        "acordo":              acordo_final,
        "confirmada":          confirmada,
        "parada_motivo":       parada_motivo,
        "tipo_veiculo":        tipo_veiculo,
        "n_cameras_votando":   n_cameras_votando,
        "fontes":              _resumo_fontes(),
        "avisos":              avisos,
        # Sai nos DOIS desfechos (com e sem placa) e sempre preenchido, inclusive
        # "completo": um campo que só aparece no modo novo obrigaria o consumidor a tratar
        # ausência como um terceiro estado.
        "modo":                perfil,
    }
