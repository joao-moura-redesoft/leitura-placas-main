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

from app.core import arquivos, banco, config
from app.integracoes import apiplacas
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

# Campos de uma ficha, em dois grupos. Ordem = ordem de exibição no editor. Tudo string
# livre: o card só mostra o texto, e combustível é rótulo ("Flex", "Gasolina", "Elétrico")
# cujo ícone o frontend escolhe. `ano` fica string por consistência (config inteiro é do
# backend real) e é convertido na fronteira, em `_curados`.

# O que o card "Bem-vindo!" da vitrine mostra. `apelido` e `mensagem` existem SÓ para a
# tela: não são campos de registro de veículo e nunca entram no bloco `veiculo` do payload.
CAMPOS_KIOSK = ("apelido", "modelo", "combustivel", "cor", "ano", "mensagem")

# O resto do registro, para o bloco `veiculo` do payload sair COMPLETO offline (ver
# `bloco_de_leitura`). Os nomes são exatamente as chaves curadas da apiplacas
# (`banco.CAMPOS_CURADOS`) de propósito: o mapeamento vira uma cópia por nome, e um campo
# novo na integração aparece aqui como campo faltando em vez de virar tradução silenciosa.
# `modelo`/`combustivel`/`cor`/`ano` já vieram do grupo de cima e servem aos dois usos.
CAMPOS_REGISTRO = ("marca", "ano_modelo", "combustivel_sigla", "especie",
                   "tipo_veiculo", "situacao", "municipio", "uf")

CAMPOS = (*CAMPOS_KIOSK, *CAMPOS_REGISTRO)

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
        # Sem repetir "Bem-vindo!": esse é o <h1> do card, e a mensagem aparece LOGO
        # ABAIXO dele na vitrine. A linha aqui serve para dizer o que a demo prova.
        "mensagem": "Leitura feita pela câmera, sem etiqueta, sem antena e sem parar o veículo.",
        # Registro: o que dá ao payload da demo a mesma cara de uma consulta real. São
        # fatos do veículo (um Nivus 2024), não do cadastro do DETRAN.
        "marca": "VW",
        "ano_modelo": "2024",
        "especie": "Passageiro",
        "tipo_veiculo": "Automovel",
        "situacao": "Sem restrição",
        # Deliberadamente EM BRANCO: município/UF de licenciamento e sigla de combustível
        # da FIPE são dados do registro que ninguém aqui conhece, e a consulta real também
        # os devolve nulos quando o registro não informa — inventar seria o único jeito de
        # o payload da demo mentir sobre algo que o posto poderia conferir. O operador
        # preenche no editor de fichas se a demo pedir.
        "combustivel_sigla": "",
        "municipio": "",
        "uf": "",
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
            log.warning("Falha ao ler %s, usando fichas padrão (%s)", caminho, e)
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
    # ATÔMICO: a vitrine (`/api/feira/scan`, ~1,5 s entre leituras) e `/feira` chamam
    # `carregar_fichas` em paralelo a este salvamento. Com `write_text` (trunca e depois
    # escreve), o `json.loads` do leitor levantava no meio da janela e `carregar_fichas`
    # caía nos PADROES — a demo perdia as fichas do operador em silêncio, na hora errada.
    # (Auditoria 05/09/2026.)
    arquivos.escrever_json_atomico(_arquivo(), limpo)
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


# ─── Ficha → bloco `veiculo` do payload ──────────────────────────────────────
# Por que isto existe: no estande NÃO HÁ INTERNET. Sem rede, sem token e com o cache
# vazio, `apiplacas.consultar` só sabe devolver `indisponivel`, e o payload da demo sairia
# sem combustível — o campo que a integração existe para entregar e o que o posto quer ver
# funcionando. A ficha local, que já alimenta o card da vitrine, passa a alimentar também
# o bloco `veiculo`, com a MESMA forma da consulta real.
#
# O que NÃO muda por causa disto: nada escreve na tabela `veiculos` (o cache real segue
# limpo de dado sintético), e o bloco sai marcado com `origem="feira"` + `motivo`
# preenchido. É a mesma disciplina do cabeçalho deste arquivo e de `feira.py` — a demo
# pode parecer produção para o consumidor, mas nunca pode ser CONFUNDIDA com produção por
# quem mede.

def _inteiro(txt: str | None) -> int | None:
    """Texto da ficha → int, ou None quando não é número.

    `ano`/`ano_modelo` são INTEIROS no bloco real (`apiplacas.normalizar` usa `_inteiro`),
    e a ficha guarda tudo como string. Sem esta conversão a demo entregaria `"2024"` onde
    produção entrega `2024` — divergência de TIPO, que é a que quebra sidecar tipado e a
    que um teste de forma por chaves não pega.
    """
    try:
        return int(str(txt).strip())
    except (TypeError, ValueError):
        return None


def _curados(ficha: dict) -> dict:
    """Ficha → as chaves de `banco.CAMPOS_CURADOS`, com os tipos do bloco real.

    Cópia por nome (os campos da ficha se chamam igual às chaves curadas), com string
    vazia virando None: no bloco real, campo ausente é `null`, e mandar `""` faria o
    consumidor tratar "não informado" como valor.
    """
    campos = {k: ((ficha.get(k) or "").strip() or None) for k in banco.CAMPOS_CURADOS}
    campos["ano"] = _inteiro(ficha.get("ano"))
    campos["ano_modelo"] = _inteiro(ficha.get("ano_modelo"))
    return campos


def bloco_de_leitura(resultado: dict) -> dict | None:
    """O bloco `veiculo` desta leitura SE ela foi mockada; None se não foi.

    None significa "siga o caminho normal da apiplacas" — é o desfecho de toda leitura
    real, inclusive a do celular do visitante, e é o que mantém o resto do payload
    idêntico ao de antes desta feature.

    O gancho é `resultado["mockada"]`, que só `app/visao/leitura.py` preenche, e só depois
    de `feira.casar` ter aprovado a placa E o posto. Reconhecer o mock por aqui, em vez de
    reexecutar `casar`, é o que impede as duas camadas de discordarem sobre o que é leitura
    de demonstração — uma decidindo pela placa e a outra pela tolerância configurada no
    meio de um evento.

    E é `mockada`, não `origem == feira.ORIGEM`: a origem também vale "feira" quando quem
    chama é a própria vitrine, e aí a placa de um visitante ganharia a ficha do carrinho.

    Não olha `apiplacas_ativo` de propósito. O recurso está desligado justamente na
    máquina da feira (não há token nem rede para justificá-lo ligado), e amarrar a demo a
    ele deixaria o payload sem `veiculo` exatamente no cenário para o qual isto foi
    escrito. O escopo continua estreito por outro caminho, mais forte que a flag: só
    chega aqui leitura já mockada, e mock só existe no posto de demonstração.
    """
    if not resultado.get("mockada") or not resultado.get("placa"):
        return None
    placa = resultado["placa"]
    ficha = ficha_de(placa)
    if ficha is None:
        log.warning("MODO FEIRA: placa mockada %s sem ficha cadastrada. O bloco 'veiculo' "
                    "sai indisponivel. Cadastre a ficha em /configuracao.", placa)
        return apiplacas.bloco_sem_ficha(
            f"veiculo de demonstracao '{placa}' sem ficha cadastrada")
    log.warning("MODO FEIRA: bloco 'veiculo' de %s montado da ficha LOCAL (MOCK). "
                "nao houve consulta a apiplacas.", placa)
    return apiplacas.bloco_demonstracao(
        _curados(ficha),
        motivo=f"dados de demonstracao da ficha local de '{placa}' (modo feira, MOCK)")
