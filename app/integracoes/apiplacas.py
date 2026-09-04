"""Consulta de dados do veículo por placa na apiplacas.com.br, com cache no banco.

O que o posto quer daqui é o TIPO DE COMBUSTÍVEL — para conferir o abastecimento contra
o que a bomba entregou. Marca/modelo/ano vêm de carona.

**Cada consulta custa crédito pré-pago.** Praticamente todo o desenho deste módulo é
gestão desse custo, em portões que `consultar` aplica nesta ordem:

  1. formato da placa (regex local)      → barra lixo antes de qualquer coisa
  2. cache no banco (`veiculos`)         → em regime, a esmagadora maioria das leituras
  3. recurso desligado / sem token       → inerte, como `email.configurado`
  4. disjuntor (402/429 ou falhas)       → não insiste no que não pode dar certo
  5. orçamento de tempo da leitura       → não empurra a resposta além do que o roteador tolera
  6. teto diário (contado no banco)      → freio de gasto que sobrevive a restart
  7. cooldown por placa                  → mata o retry do roteador no mesmo abastecimento
  8. teto por minuto                     → freio de rajada

Duas propriedades dessa ordem não são estéticas:

- **O cache vem antes de todos os freios (2 antes de 4-8).** Os freios existem para limitar
  GASTO, e um cache hit não gasta. Um freio que barra leitura de cache transforma
  "economizei" em "perdi o dado".
- **Os portões que só LEEM estado (1-6) vêm antes dos que CONSOMEM (7-8).**
  `limitador.permitido` registra a chamada quando devolve True, então um portão consumidor
  posicionado antes de um portão que ainda pode abortar queima cota sem ter gasto nada — no
  caso do cooldown por placa, deixaria o posto até 10 minutos sem o combustível daquela
  placa, em silêncio. Ver `test_desistir_por_falta_de_tempo_nao_queima_o_cooldown`.

Contrato da API (doc oficial, 24/08/2026):
  GET {base}/consulta/{placa}/{token}  → 200 com os dados, ou 401/402/406/429
  GET {base}/saldo/{token}             → {"qtdConsultas": N}

O TOKEN VAI NO PATH DA URL. Nenhum log deste módulo pode conter a URL montada — ver
`_url_segura`.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone

from app.core import banco, config
from app.seguranca import limitador
from app.visao.validador import RE_ANTIGO, RE_MERCOSUL

log = logging.getLogger(__name__)

# Vereditos do bloco `veiculo` entregue no payload. `indisponivel` é distinto de
# `inexistente` de propósito: "não conseguimos consultar" (reconsultar amanhã resolve)
# não é "esse veículo não existe na base" (reconsultar é inútil para sempre). O repo se
# recusa a fundir "não sei" com um valor em todo lugar — `tipo_veiculo`, `confirmada` —
# e este é o mesmo caso.
CONSULTA_OK = "ok"
CONSULTA_INEXISTENTE = "inexistente"
CONSULTA_INDISPONIVEL = "indisponivel"

# Valores de `veiculo.origem`. "api" = consulta paga agora; "cache" = nosso banco;
# `ORIGEM_DEMONSTRACAO` = ficha local do modo feira (dado sintético, ver `bloco_demonstracao`).
#
# Literal, e não `from app.visao import feira`: este módulo não tem nada a ver com o modo
# de demonstração e não deve passar a depender dele para nomear um rótulo próprio. O que
# garante que os dois não divirjam é `test_declara_que_e_demonstracao`, que prende os dois
# ao mesmo valor — um assert de uma linha custa menos que a aresta no grafo de imports.
ORIGEM_DEMONSTRACAO = "feira"

# Chaves do bloco `veiculo` do payload. SEMPRE todas presentes, em todos os desfechos:
# um bloco que muda de forma é o pior caso para o sidecar Java tipado que consome isso.
CHAVES_VEICULO = (
    "consulta", "origem", "consultado_em", "motivo",
    *banco.CAMPOS_CURADOS,
)

# Janela do cooldown por placa. O comentário de `atualizar_deteccao` documenta o roteador
# chamando 3x em 140s para o MESMO veículo (retry no mesmo abastecimento); sem este freio,
# uma placa que deu timeout na primeira é recomprada a cada retry.
_JANELA_POR_PLACA_SEG = 600

# Disjuntor: dict de módulo sob lock, no idioma de `seguranca/limitador.py` e
# `seguranca/sessao.py` ("dict protegido por lock, sem dependência externa").
_lock = threading.Lock()
_pausa_ate: float = 0.0
_pausa_motivo: str = ""
_falhas_seguidas: int = 0

# Falhas de TRANSPORTE seguidas antes de parar de tentar, e por quanto tempo. Distinto do
# limitador de propósito: o limitador conta TENTATIVAS e estrangularia uma API saudável
# com o mesmo rigor; aqui só falha conta.
_MAX_FALHAS_SEGUIDAS = 3
_PAUSA_FALHAS_SEG = 60


def _url_segura(url: str) -> str:
    """A URL com o token trocado por `***`, para poder aparecer em log.

    O token é o último segmento do path (`/consulta/{placa}/{token}`), então logar a URL
    crua — o reflexo natural num `except` — vaza a credencial paga para o arquivo de log,
    de onde ela sai em qualquer suporte, backup ou colagem de diagnóstico.
    """
    return re.sub(r"/[^/]*$", "/***", url)


def normalizar_placa(placa: str) -> str:
    """Maiúscula, só alfanumérico. É a chave do cache e tem de ser única.

    A comparação de TEXT PRIMARY KEY no SQLite é BINÁRIA: sem isto, "abc1d23" e
    "ABC1D23" seriam duas linhas e DUAS COBRANÇAS pelo mesmo veículo.
    """
    return re.sub(r"[^A-Z0-9]", "", (placa or "").upper())


def placa_consultavel(placa: str) -> bool:
    """A placa tem forma que a API aceita (AAA0000 ou AAA0A00)?

    Reusa as regex de `app/visao/validador.py` em vez de escrever outras — são a mesma
    definição de "placa brasileira" que o OCR já aplica. Evita gastar uma consulta para
    receber HTTP 401 "placa inválida".
    """
    return bool(RE_ANTIGO.match(placa) or RE_MERCOSUL.match(placa))


# ─── Disjuntor ─────────────────────────────────────────────────────────────

def _pausado() -> str:
    """O motivo da pausa, ou "" se está liberado."""
    with _lock:
        if _pausa_ate > time.monotonic():
            return _pausa_motivo
        return ""


def _pausar(segundos: float, motivo: str) -> None:
    global _pausa_ate, _pausa_motivo
    with _lock:
        ja_pausado = _pausa_ate > time.monotonic()
        _pausa_ate = time.monotonic() + segundos
        _pausa_motivo = motivo
    # Log só na transição: 402/429 a cada leitura de cada posto encheria o log em minutos,
    # e é justamente o alerta que precisa ser visível para não passar batido.
    if not ja_pausado:
        log.error("apiplacas: pausando consultas por %ds — %s", segundos, motivo)


def _contar_falha(motivo: str) -> None:
    """Falha de transporte: pausa curta só depois de `_MAX_FALHAS_SEGUIDAS` seguidas.

    Uma falha isolada não vale pausa (a próxima leitura pode funcionar), mas o fornecedor
    fora por horas cobraria o timeout de toda leitura com cache miss.
    """
    global _falhas_seguidas
    with _lock:
        _falhas_seguidas += 1
        estourou = _falhas_seguidas >= _MAX_FALHAS_SEGUIDAS
    if estourou:
        _pausar(_PAUSA_FALHAS_SEG, f"{_MAX_FALHAS_SEGUIDAS} falhas seguidas ({motivo})")


def _zerar_falhas() -> None:
    global _falhas_seguidas
    with _lock:
        _falhas_seguidas = 0


def limpar_pausa() -> None:
    """Libera o disjuntor agora.

    Chamada quando o token muda em `/configuracao`: sem isso, corrigir um token errado só
    surtiria efeito quando a pausa expirasse, e o operador concluiria — com razão, do
    ponto de vista dele — que a tela não funciona.
    """
    global _pausa_ate, _pausa_motivo, _falhas_seguidas
    with _lock:
        _pausa_ate = 0.0
        _pausa_motivo = ""
        _falhas_seguidas = 0


# ─── Normalização da resposta (pura: sem rede, sem banco) ──────────────────

def _obj(v) -> dict:
    """`v` se for um objeto JSON, senão `{}`.

    `v or {}` — o idioma óbvio — só protege contra `None` e contra vazio: uma LISTA não
    vazia passa direto e estoura no `.get` seguinte. Isso não é hipótese defensiva à toa;
    é o formato de um terceiro que pode mudar sem avisar, e uma exceção aqui perde uma
    consulta que já foi PAGA (o dado não é entregue nem cacheado).
    """
    return v if isinstance(v, dict) else {}


def _texto(v) -> str | None:
    """Texto limpo, ou None. Nunca "" — duas representações de "não sei" no payload é o
    que faz o consumidor errar.

    Recusa dict/list em vez de aceitar o `str()` deles: se o fornecedor trocar um campo
    de texto por um objeto, `str(v)` colocaria um repr de Python (`"{'a': 1}"`) no payload
    entregue ao posto, que é pior que não informar nada.
    """
    if v is None or isinstance(v, (dict, list, tuple, set)):
        return None
    s = str(v).strip()
    return s or None


def _inteiro(v) -> int | None:
    """A API manda ano como string ("2007"). Tolerante: "", "-" e lixo viram None."""
    s = _texto(v)
    if s is None:
        return None
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def _primeiro(d: dict, *chaves: str):
    """O primeiro valor não vazio entre `chaves`.

    A resposta traz a mesma informação em caixas diferentes (`MARCA` e `marca`, `MODELO`
    e `modelo`). A doc lista as duas famílias; apostar numa só é apostar em qual o
    fornecedor vai manter.
    """
    for k in chaves:
        v = _texto(d.get(k))
        if v is not None:
            return v
    return None


def _melhor_fipe(bruto: dict) -> dict:
    """A entrada FIPE de maior `score`, ou {} se não houver nenhuma utilizável.

    A doc manda escolher pelo maior `score` (ele indica a qualidade da correspondência
    nome/marca) e avisa que pode haver MÚLTIPLOS valores na mesma consulta. Pegar `[0]`
    devolveria a versão errada num modelo com oito versões.
    """
    itens = _obj(bruto.get("fipe")).get("dados")
    validos = [d for d in itens if isinstance(d, dict)] if isinstance(itens, list) else []
    if not validos:
        return {}

    def _score(d: dict) -> float:
        try:
            return float(d.get("score"))
        except (TypeError, ValueError):
            return float("-inf")

    return max(validos, key=_score)


def normalizar(bruto: dict) -> dict:
    """Resposta 200 da apiplacas → bloco curado (chaves de `banco.CAMPOS_CURADOS`).

    Pura de propósito: é a regra que decide o campo mais importante da integração, e dá
    para testá-la com um dict colado da documentação.

    Toda leitura é defensiva porque a própria doc avisa que `extra` pode vir AUSENTE OU
    INCOMPLETO e que `fipe` pode faltar — e `extra` é exatamente onde vive o combustível.
    """
    # `_obj` e não `.get("extra", {})` nem `or {}`: a chave pode vir presente e `null`,
    # e também pode vir com o tipo errado (ver `_obj`).
    extra = _obj(bruto.get("extra"))
    fipe = _melhor_fipe(bruto)

    # `extra.combustivel` ("Alcool / Gasolina") ganha do da FIPE ("Gasolina") por motivo
    # semântico: a FIPE descreve a VERSÃO precificada, o `extra` descreve o VEÍCULO. Num
    # flex a FIPE responde a pergunta errada — e flex é justamente o que o posto precisa
    # saber para conferir o abastecimento.
    combustivel = _texto(extra.get("combustivel")) or _texto(fipe.get("combustivel"))

    # A sigla sai SÓ da FIPE: `extra` não tem sigla, e derivá-la do texto seria inventar
    # o dado. "Alcool / Gasolina" não tem sigla única, e cravar "G" afirmaria que o carro
    # é a gasolina — errando exatamente o caso que motivou a feature.
    sigla = _texto(fipe.get("sigla_combustivel"))

    return {
        "combustivel":       combustivel,
        "combustivel_sigla": sigla,
        "marca":             _primeiro(bruto, "marca", "MARCA") or _texto(extra.get("marca")),
        "modelo":            _primeiro(bruto, "modelo", "MODELO", "SUBMODELO")
                             or _texto(extra.get("modelo")),
        "ano":               _inteiro(bruto.get("ano") or extra.get("ano_fabricacao")),
        "ano_modelo":        _inteiro(bruto.get("anoModelo") or extra.get("ano_modelo")),
        "cor":               _primeiro(bruto, "cor", "COR"),
        "especie":           _texto(extra.get("especie")),
        "tipo_veiculo":      _texto(extra.get("tipo_veiculo")),
        "situacao":          _texto(bruto.get("situacao")) or _texto(extra.get("situacao_veiculo")),
        "municipio":         _primeiro(bruto, "municipio") or _texto(extra.get("municipio")),
        "uf":                _primeiro(bruto, "uf") or _texto(extra.get("uf")),
    }


# ─── Fronteira HTTP (o ponto que os testes substituem) ─────────────────────

def buscar_na_api(placa: str, token: str, timeout_seg: float,
                  base_url: str) -> tuple[int | None, dict | None, str]:
    """ÚNICA função do sistema que fala com a apiplacas. NUNCA levanta.

    Devolve `(http_status, corpo_json, erro)`:
      - `http_status=None` = não houve resposta (timeout, DNS, conexão recusada). É
        DISTINTO de `406` (a API respondeu, e a resposta é "não existe"): fundir os dois
        faria uma queda de rede virar cache negativo por 30 dias.
      - `corpo_json` = o corpo desserializado, quando havia e era JSON.

    É a fronteira monkeypatchável, no molde de `app/seguranca/email.py:enviar` — que é
    como `testes/unitarios/test_conta.py` neutraliza o SMTP. Nada acima daqui conhece
    `requests`.

    `import requests` local e `except Exception` amplo: mesmo padrão do único outro HTTP
    de saída do projeto (`app/visao/pipeline.py:_enviar_webhook`).

    Sem retry: a única falha que um retry consertaria é rede intermitente, e não cabe uma
    segunda tentativa no orçamento de latência da leitura reativa. A placa que falhou hoje
    é reconsultada no próximo abastecimento — de graça, porque falha não vira cache.
    """
    url = f"{base_url.rstrip('/')}/consulta/{placa}/{token}"
    try:
        import requests
        # timeout separado (conexão, leitura): um provedor que aceita a conexão e pendura
        # a resposta é o caso comum, e só o segundo valor o cobre.
        r = requests.get(url, timeout=(min(timeout_seg, 2.0), timeout_seg))
        try:
            corpo = r.json()
        except ValueError:
            corpo = None
        if r.status_code == 200:
            return 200, corpo, ""
        # A doc diz que todo erro vem com {"message": "..."}.
        msg = (corpo or {}).get("message") if isinstance(corpo, dict) else None
        return r.status_code, corpo, _texto(msg) or f"HTTP {r.status_code}"
    except Exception as e:
        # `_url_segura` e não `url`: o token está no path.
        log.warning("apiplacas: falha ao consultar %s (%s): %s",
                    placa, _url_segura(url), type(e).__name__)
        return None, None, f"{type(e).__name__}"


def saldo(cfg: dict | None = None) -> int | None:
    """`GET /saldo/{token}` → `qtdConsultas`, ou None se não deu para saber.

    Nunca é chamada do caminho da leitura — só por rota de admin, e com freio próprio:
    é uma chamada externa atrás de um botão de painel, e botão de painel é clicado em
    sequência.
    """
    cfg = cfg or config.carregar()
    token = (cfg.get("apiplacas_token") or "").strip()
    if not token:
        return None
    if not limitador.permitido("apiplacas_saldo", "*", 6, 3600):
        return None
    base = (cfg.get("apiplacas_url") or "").strip() or "https://wdapi2.com.br"
    url = f"{base.rstrip('/')}/saldo/{token}"
    try:
        import requests
        r = requests.get(url, timeout=(2.0, 5.0))
        if r.status_code != 200:
            log.warning("apiplacas: saldo devolveu HTTP %s", r.status_code)
            return None
        return int((r.json() or {}).get("qtdConsultas"))
    except Exception as e:
        log.warning("apiplacas: falha ao consultar saldo (%s): %s",
                    _url_segura(url), type(e).__name__)
        return None


# ─── Orquestração ──────────────────────────────────────────────────────────

def configurado(cfg: dict | None = None) -> bool:
    """Recurso ligado E com token. Vazio = inerte, como `email.configurado`."""
    cfg = cfg or config.carregar()
    return (config.get_bool(cfg, "apiplacas_ativo")
            and bool((cfg.get("apiplacas_token") or "").strip()))


def _bloco(consulta: str, motivo: str = "", origem: str | None = None,
           consultado_em: str | None = None, campos: dict | None = None) -> dict:
    """Bloco `veiculo` com as chaves SEMPRE completas, faltando o que faltar."""
    campos = campos or {}
    return {
        "consulta": consulta,
        "origem": origem,
        "consultado_em": consultado_em,
        "motivo": motivo,
        **{k: campos.get(k) for k in banco.CAMPOS_CURADOS},
    }


def _do_cache(linha: dict) -> dict:
    consulta = CONSULTA_OK if linha["status"] == "ok" else CONSULTA_INEXISTENTE
    motivo = "" if consulta == CONSULTA_OK else "placa não consta na base consultada"
    return _bloco(consulta, motivo, origem="cache",
                  consultado_em=linha["consultado_em"], campos=linha)


# ─── Bloco de DEMONSTRAÇÃO (modo feira) ──────────────────────────────────────
# O evento acontece OFFLINE: não há internet, não há token e o cache está vazio, então
# `consultar` só sabe devolver `indisponivel` — e o payload da demo sairia sem combustível,
# que é justamente o campo que a integração existe para entregar. As duas funções abaixo
# montam o mesmo bloco a partir da ficha local (`app/visao/feira_fichas.py`).
#
# Moram AQUI, e não no módulo da feira, porque a FORMA do bloco é deste módulo: `_bloco`
# garante as `CHAVES_VEICULO` todas presentes em todos os desfechos, e é o que o sidecar
# Java tipado consome. Um segundo lugar montando o dicionário à mão divergiria no primeiro
# campo novo — o mesmo motivo do "um lugar só, para não divergir" de `_tratar_resposta`.
#
# Nenhuma das duas escreve na tabela `veiculos`: dado sintético não entra no cache real.
# Ver o cabeçalho de `feira_fichas.py` — é a mesma linha que separa a demo da medição.

def bloco_demonstracao(campos: dict, motivo: str) -> dict:
    """Bloco `veiculo` de DEMONSTRAÇÃO, com a forma exata da consulta real.

    `consulta="ok"` porque, para o consumidor, o desfecho É um registro encontrado — é o
    que faz o sidecar do posto exercitar o mesmo caminho de código que exercitaria em
    produção, que é o ponto de demonstrar.

    O que impede isso de virar mentira é `origem`: `ORIGEM_DEMONSTRACAO` não é `"api"` nem
    `"cache"`, então quem quiser distinguir dado real de dado de estande tem um campo para
    olhar — e `motivo` vem preenchido mesmo no sucesso (a consulta real deixa vazio),
    dizendo em texto que a origem é a ficha local.

    `consultado_em` é o instante da montagem: o dado foi de fato obtido agora, da ficha.
    """
    return _bloco(CONSULTA_OK, motivo, origem=ORIGEM_DEMONSTRACAO,
                  consultado_em=datetime.now(timezone.utc).isoformat(),
                  campos=campos)


def bloco_sem_ficha(motivo: str) -> dict:
    """Bloco `veiculo` para leitura mockada que NÃO tem ficha cadastrada.

    `indisponivel` e não `ok` com tudo nulo: a segunda forma afirmaria que o registro
    existe e não informou nada, e a doc manda ler campo nulo exatamente assim. Aqui o
    registro não existe — falta cadastro, e `motivo` diz isso.

    Sai como bloco em vez de `None` para o desfecho aparecer: `app/web/leitura.py` promove
    `consulta="indisponivel"` ao motivo da chamada, então "esqueci de preencher a ficha do
    carrinho" viraria uma linha no painel em vez de um card vazio descoberto na feira.
    """
    return _bloco(CONSULTA_INDISPONIVEL, motivo, origem=ORIGEM_DEMONSTRACAO)


def _meia_noite_utc() -> str:
    return datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).isoformat()


def consultar(placa: str, cfg: dict | None = None, *,
              permitir_gasto: bool = True,
              orcamento_seg: float | None = None) -> dict:
    """Bloco `veiculo{}` pronto para o payload. NUNCA levanta.

    `permitir_gasto=False` é o modo CACHE-ONLY, usado pelo botão "Testar como o roteador",
    pelo editor de ROI e por `GET /api/placa/{placa}`. É palavra-chave obrigatória e sem
    default implícito no chamador de propósito: cada ponto do código declara se pode
    gastar dinheiro, e isso aparece na revisão do diff.

    `orcamento_seg` limita a espera ao que sobrou do tempo da leitura — a chamada externa
    não deve empurrar a resposta para além do que o roteador tolera.
    """
    cfg = cfg or config.carregar()
    placa = normalizar_placa(placa)

    if not placa:
        return _bloco(CONSULTA_INDISPONIVEL, "sem placa para consultar")
    if not placa_consultavel(placa):
        # Não gasta: a API devolveria 401 "placa inválida" e cobraria a tentativa.
        return _bloco(CONSULTA_INDISPONIVEL, "placa fora dos formatos AAA0000/AAA0A00")

    ttl = config.get_int(cfg, "apiplacas_ttl_dias")
    ttl_neg = config.get_int(cfg, "apiplacas_ttl_negativo_dias")

    # ── Cache primeiro, e antes de QUALQUER freio: freio existe para limitar gasto, e
    # cache hit não gasta.
    try:
        linha = banco.veiculos_valido(placa, ttl, ttl_neg)
    except Exception as e:
        log.warning("apiplacas: falha ao ler cache de %s: %s", placa, e)
        linha = None
    if linha is not None:
        return _do_cache(linha)

    if not permitir_gasto:
        return _bloco(CONSULTA_INDISPONIVEL,
                      "sem dados em cache (este fluxo não consulta a API paga)")
    if not configurado(cfg):
        return _bloco(CONSULTA_INDISPONIVEL, "consulta de veículo não configurada")

    pausa = _pausado()
    if pausa:
        return _bloco(CONSULTA_INDISPONIVEL, f"consulta pausada: {pausa}")

    # ORDEM IMPORTA DAQUI PARA BAIXO: `limitador.permitido` CONSOME o slot quando devolve
    # True (ver a docstring dele), então todo portão que só LÊ estado tem de vir antes.
    # Com a ordem invertida, uma consulta abortada por falta de orçamento de tempo — que
    # não gastou centavo nenhum — queimaria o cooldown de 10 min daquela placa, e a
    # próxima leitura seria recusada com "placa já consultada há instantes" sem nunca ter
    # sido consultada.
    timeout = config.get_float(cfg, "apiplacas_timeout_seg")
    if orcamento_seg is not None:
        timeout = min(timeout, orcamento_seg)
        if timeout <= 0.2:
            # A leitura já queimou o tempo; consultar agora empurraria a resposta para
            # além do que o roteador tolera. O dado aparece na próxima leitura.
            return _bloco(CONSULTA_INDISPONIVEL, "sem orçamento de tempo para consultar")

    por_dia = config.get_int(cfg, "apiplacas_max_por_dia")
    if por_dia > 0:
        try:
            if banco.veiculos_consultas_desde(_meia_noite_utc()) >= por_dia:
                return _bloco(CONSULTA_INDISPONIVEL, "teto de consultas do dia atingido")
        except Exception as e:
            log.warning("apiplacas: falha ao contar consultas do dia: %s", e)

    # A partir daqui os portões consomem. O de placa vem primeiro porque é o mais
    # específico: se ele barrar, não faz sentido gastar uma vaga do teto por minuto.
    if not limitador.permitido("apiplacas_placa", placa, 1, _JANELA_POR_PLACA_SEG):
        # Retry do roteador no mesmo abastecimento: a 1ª tentativa já falhou/consultou.
        return _bloco(CONSULTA_INDISPONIVEL, "placa já consultada há instantes")

    por_min = config.get_int(cfg, "apiplacas_max_por_minuto")
    if por_min > 0 and not limitador.permitido("apiplacas_min", "*", por_min, 60):
        return _bloco(CONSULTA_INDISPONIVEL, "teto de consultas por minuto atingido")

    base = (cfg.get("apiplacas_url") or "").strip() or "https://wdapi2.com.br"
    token = (cfg.get("apiplacas_token") or "").strip()
    http, corpo, erro = buscar_na_api(placa, token, timeout, base)

    return _tratar_resposta(placa, http, corpo, erro, cfg)


def _tratar_resposta(placa: str, http: int | None, corpo: dict | None,
                     erro: str, cfg: dict) -> dict:
    """Mapa único de desfecho → (cacheia?, pausa?, bloco). Um lugar só, para não divergir."""
    pausa_erro = config.get_int(cfg, "apiplacas_pausa_erro_seg")

    if http == 200 and isinstance(corpo, dict):
        _zerar_falhas()
        campos = normalizar(corpo)
        if not any(v is not None for v in campos.values()):
            # 200 sem UM campo sequer não é registro de veículo — é quase certamente um
            # erro devolvido com status 200 (`{"message": ...}`), coisa que APIs fazem.
            # Cachear isso como 'ok' seria pior que perder a consulta: o posto receberia
            # `consulta: "ok"` com `combustivel: null` — que a doc manda ler como "o
            # registro não informou" — e ficaria assim pelos próximos 180 dias, sem
            # nenhum sintoma além do combustível que nunca vem.
            msg = _texto(corpo.get("message")) or "resposta vazia"
            log.error("apiplacas: 200 sem dados úteis para %s (%s) — não cacheado", placa, msg)
            return _bloco(CONSULTA_INDISPONIVEL, "consulta devolveu resposta sem dados")
        try:
            banco.veiculos_salvar(placa, status="ok", campos=campos,
                                  bruto=json.dumps(corpo, ensure_ascii=False),
                                  http_status=200)
            linha = banco.veiculos_obter(placa)
            quando = linha["consultado_em"] if linha else None
        except Exception as e:
            # Gravar falhou, mas a consulta foi PAGA: entregar o dado é melhor que
            # descartá-lo. A próxima leitura reconsulta (e repaga) — e o log diz por quê.
            log.error("apiplacas: consulta paga de %s não pôde ser gravada: %s", placa, e)
            quando = None
        log.info("apiplacas: %s consultada — combustivel=%r", placa, campos["combustivel"])
        return _bloco(CONSULTA_OK, "", origem="api", consultado_em=quando, campos=campos)

    if http == 406:
        # Resposta LEGÍTIMA: a placa não consta na base. Cacheada para não repagar em todo
        # abastecimento — o caso comum é OCR que leu errado, uma placa que nunca existirá.
        _zerar_falhas()
        try:
            banco.veiculos_salvar(placa, status="inexistente", campos={},
                                  bruto=None, http_status=406)
            linha = banco.veiculos_obter(placa)
            quando = linha["consultado_em"] if linha else None
        except Exception as e:
            log.error("apiplacas: negativa de %s não pôde ser gravada: %s", placa, e)
            quando = None
        return _bloco(CONSULTA_INEXISTENTE, "placa não consta na base consultada",
                      origem="api", consultado_em=quando)

    if http == 402:
        # Token inválido: retentar não pode dar certo. Pausa longa (×4) porque só um
        # humano trocando o token no painel resolve — e `limpar_pausa` cobre esse caso.
        _pausar(pausa_erro * 4, "token inválido (HTTP 402)")
        return _bloco(CONSULTA_INDISPONIVEL, "consulta sem credencial válida")

    if http == 429:
        _pausar(pausa_erro, "limite de consultas atingido (HTTP 429)")
        return _bloco(CONSULTA_INDISPONIVEL, "consulta sem saldo (limite atingido)")

    if http == 401:
        # Placa recusada pelo formato. É bug NOSSO (passou por `placa_consultavel`) —
        # warning, e sem pausa: o problema é a placa, não o provedor.
        log.warning("apiplacas: placa %s recusada pela API (401): %s", placa, erro)
        return _bloco(CONSULTA_INDISPONIVEL, "placa recusada pela consulta (formato)")

    if http == 400:
        log.error("apiplacas: chamada malformada para %s (400): %s", placa, erro)
        _pausar(60, "chamada malformada (HTTP 400)")
        return _bloco(CONSULTA_INDISPONIVEL, "erro na chamada da consulta")

    # Sem resposta (timeout/rede) ou 5xx: falha de TRANSPORTE. Não vira cache — senão um
    # minuto ruim do fornecedor marcaria placas como inexistentes por 30 dias.
    _contar_falha(erro or (f"HTTP {http}" if http else "sem resposta"))
    return _bloco(CONSULTA_INDISPONIVEL, "consulta indisponível (tempo esgotado ou erro de rede)")
