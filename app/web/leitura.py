"""GET /api/leitura — endpoint reativo multi-tenant.

O roteador do posto (sidecar Java, fora do nosso escopo) chama este endpoint quando um
abastecimento TERMINA, passando entidade/cnpj/automacao/bico. Localizamos a câmera+ROI
do bico, tiramos uma foto fresca agora e devolvemos a placa lida — nada de pipeline
contínuo envolvido.
"""
from __future__ import annotations
import secrets
import json
import logging
import re
import time

from fastapi import APIRouter, HTTPException, Query, Request

from app.core import banco
from app.seguranca import sessao as auth_mod
from app.core import config
from app.core import estado
from app.seguranca import limitador
from app.integracoes import apiplacas
from app.visao import leitura
from app.visao import pipeline

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

# Idade máxima do frame do pipeline para servir a uma leitura reativa. Acima disso o
# pipeline provavelmente está reconectando e o frame ficou velho — aí vale mais abrir
# conexão direta do que responder com uma foto defasada.
FRAME_MAX_IDADE_SEG = 2.0


# Espera pelo primeiro frame quando o pipeline ainda está aquecendo (conexão RTSP +
# carga dos modelos levam dezenas de segundos após o boot).
ESPERA_PRIMEIRO_FRAME_SEG = 20.0


def leituras_ao_vivo(camera_id: int):
    """Provedor das leituras que o pipeline continuo JA fez desta camera, ou None.

    Par do `frame_ao_vivo`, e pelo mesmo motivo de fundo: o que o pipeline tem em maos e
    melhor do que o que a chamada do bico consegue colher sozinha. O `frame_ao_vivo` evita
    abrir uma segunda conexao RTSP; este evita jogar fora leitura de OCR ja paga.

    Em 24/08/2026, no bico 3 do ALTIPLANO, o tracker havia lido `RLX2A77` com confianca 0,96
    e todos os char_probs >= 0,93 sete segundos antes da chamada. O GET conseguiu sondar 2
    dos 12 frames do orcamento antes do timeout de 28 s, votou so entre esses dois, e emitiu
    `HDX2477`. A evidencia certa estava a um atributo de distancia.
    """
    if camera_id not in pipeline._instancias:
        return None

    def _obter():
        # Releitura do dicionario a cada chamada: o pipeline pode ter sido derrubado e
        # recriado entre a montagem das fontes e o laco de leitura, e guardar a instancia
        # aqui deixaria este provider falando com um objeto morto.
        inst = pipeline._instancias.get(camera_id)
        tracker = getattr(inst, "tracker", None) if inst else None
        if tracker is None or not hasattr(tracker, "leituras_recentes"):
            return []
        try:
            return tracker.leituras_recentes()
        except Exception as e:
            # Mesma politica do `frame_ao_vivo`: isto e conveniencia, nunca requisito. Uma
            # falha aqui significa "sem leitura do continuo", e a chamada segue com as
            # fotos que ela mesma tirar.
            log.debug("cam=%s: leituras do pipeline indisponiveis (%s)", camera_id, e)
            return []

    return _obter


def frame_ao_vivo(camera_id: int, espera_primeiro_frame: float = ESPERA_PRIMEIRO_FRAME_SEG):
    """Provedor de frame do pipeline contínuo, ou None se a câmera não estiver com ele.

    Preferir o pipeline não é otimização: a câmera Intelbras aceita UMA conexão RTSP.
    Se o pipeline está com ela aberta (modo ao vivo), abrir uma segunda conexão falha
    — foi o que derrubava a leitura. E o frame do pipeline é mais recente que o de uma
    conexão nova, que gasta 2-3s só no handshake.
    """
    if camera_id not in pipeline._instancias:
        return None   # câmera livre → o chamador abre conexão direta
    log.debug("cam=%s: pipeline ativo, leitura usará o frame ao vivo", camera_id)

    primeira = [True]
    ultimo_devolvido = [None]

    def _obter():
        # Na primeira chamada aguarda o pipeline aquecer. Sem isso a leitura desistiria
        # do pipeline e cairia numa conexão direta que não pode dar certo (câmera ocupada).
        # No perfil rápido a espera é curta (`rapido_espera_frame_seg`) e o fallback direto
        # é suprimido — ver `_abrir_fontes`: lá desistir cedo é melhor que esperar 20s.
        limite = time.time() + (espera_primeiro_frame if primeira[0] else 0)
        primeira[0] = False

        # O pipeline agora publica frame novo na cadência de DETECÇÃO (deteccao_fps_max,
        # tipicamente 5/s = 200ms — ver app/visao/pipeline.py:_loop), mais devagar que o
        # antigo ritmo de camera_fps (15/s = 66ms). `ler_placa` chama este provider a
        # cada ~150ms (app/visao/leitura.py): sem este teto, boa parte das chamadas
        # pegaria o MESMO objeto de frame já visto na tentativa anterior — duas "fotos"
        # idênticas do reject-retry loop concordam 100% entre si e disparam a parada
        # antecipada por consenso sem NENHUMA concordância entre frames de verdade (ver
        # comentário em `_eleger_placa`, app/visao/leitura.py). Por isso espera um
        # objeto DIFERENTE do último devolvido, com teto curto — e se o teto estourar,
        # devolve None (não o duplicado): `ler_placa` já trata None dormindo 0.1s e
        # tentando de novo, SEM contar como tentativa nem como voto. Devolver o
        # duplicado, ao contrário, anularia o próprio propósito desta correção.
        pinst = pipeline._instancias.get(camera_id)
        teto_frame_novo = time.time() + (pinst._intervalo_deteccao * 1.5 if pinst else 0.3)

        while True:
            idade = time.time() - estado.ultimo_frame_ts.get(camera_id, 0)
            if idade <= FRAME_MAX_IDADE_SEG:
                # frame LIMPO: o anotado tem bbox/label desenhados e o OCR leria o overlay
                f = estado.obter_frame_camera_limpo(camera_id)
                if f is None:
                    f = estado.obter_frame_camera(camera_id)
                if f is not None:
                    if f is not ultimo_devolvido[0]:
                        ultimo_devolvido[0] = f
                        return f
                    if time.time() >= teto_frame_novo:
                        return None   # ver comentário acima — nunca devolve duplicado
                    # Frame fresco, mas ainda o mesmo objeto: continua esperando um
                    # novo, limitado só por `teto_frame_novo` — NÃO cai no `limite`
                    # abaixo, que existe pra outra coisa (pipeline sem frame nenhum).
                    time.sleep(0.05)
                    continue
            if time.time() >= limite:
                log.warning("frame_ao_vivo cam=%s: desistiu (idade do ultimo frame=%.1fs, "
                            "limpo=%s, anotado=%s)", camera_id, idade,
                            estado.obter_frame_camera_limpo(camera_id) is not None,
                            estado.obter_frame_camera(camera_id) is not None)
                return None
            time.sleep(0.05 if pinst else 0.2)

    return _obter


def montar_fontes(fontes_db: list[dict], cfg: dict,
                  perfil: str = leitura.PERFIL_COMPLETO) -> list[leitura.FonteLeitura]:
    """Traduz as câmeras do bico (vindas de `banco.cameras_do_bico`) em fontes de leitura.

    Ponto único onde a camada web liga cadastro e visão: cada câmera ganha sua
    especificação de conexão, seu ROI e seu provedor de frame ao vivo. Os dois chamadores
    de `ler_placa` (GET do roteador e botão de teste) passam por aqui para não divergirem.

    `perfil` só é usado para dimensionar a espera pelo primeiro frame — o resto do
    orçamento é decidido dentro de `ler_placa`. Ele precisa chegar até aqui porque essa
    espera está do lado de fora do laço, no provider, e nenhum timeout de lá a alcança.
    """
    espera = leitura.espera_frame_do_perfil(cfg, perfil, ESPERA_PRIMEIRO_FRAME_SEG)
    fontes = []
    for f in fontes_db:
        fontes.append(leitura.FonteLeitura(
            camera_id=f["camera_id"],
            papel=f["papel"],
            especificacao=leitura.EspecificacaoCamera.de_camera_db(f["camera"], cfg),
            roi=json.loads(f["roi"]) if f.get("roi") else None,
            provider=frame_ao_vivo(f["camera_id"], espera),
            leituras_provider=leituras_ao_vivo(f["camera_id"]),
        ))
    return fontes


# Limites do endpoint reativo — ele é PÚBLICO por design (rede interna do sidecar Java,
# ver app/servidor.py), então isto não é controle de acesso, é só um freio contra
# varredura/abuso (cnpj/automacao/bico errados de propósito, tentando descobrir cadastro
# válido). Generoso o bastante para não incomodar tráfego real: um posto reabastece bem
# menos que isso por minuto, mesmo com retries do roteador.
_LIMITE_LEITURA_IP_MIN = 60
_LIMITE_LEITURA_CNPJ_MIN = 30


def perfil_pedido(rapido: bool, cfg: dict) -> tuple[str, str]:
    """(perfil a usar, aviso para o chamador) a partir do `rapido=1` da query.

    Separada e pura pelo mesmo motivo de `_pode_gastar` e `_status_da_leitura`: é a regra
    que decide quanto tempo a chamada vai durar e quanta acurácia ela abre mão, e tem de
    ser testável sem câmera, sem rede e sem banco.

    `rapido_ativo=nao` no servidor NÃO é erro para quem chamou — é o interruptor para
    desligar o perfil leve num posto onde ele se mostrou ruim demais, sem precisar mexer
    no roteador. A chamada roda completa e o aviso explica por que demorou.
    """
    if not rapido:
        return leitura.PERFIL_COMPLETO, ""
    # Chave ausente cai no default de `config.PADROES`, e nao em False. `config.carregar`
    # sempre parte de PADROES, entao na pratica ela existe — mas `get_bool` devolve False
    # para chave que falta, e "faltou a chave" desligando um recurso em silencio e um
    # diagnostico caro. Aqui ausencia significa o default declarado, que e o que o
    # operador leria em `config.py` se fosse conferir.
    ativo = cfg.get("rapido_ativo", config.PADROES.get("rapido_ativo", "sim"))
    if not config.get_bool({"rapido_ativo": ativo}, "rapido_ativo"):
        return (leitura.PERFIL_COMPLETO,
                "modo rápido pedido mas desativado neste servidor — leitura completa")
    return leitura.PERFIL_RAPIDO, ""


def _pode_gastar(resultado: dict, cfg: dict) -> bool:
    """Se esta leitura merece uma consulta PAGA à apiplacas.

    Separada e pura pelo mesmo motivo de `_status_da_leitura`: é a regra que gasta
    dinheiro do cliente, e tem de ser testável sem câmera, sem rede e sem banco.

    `apiplacas_modo` decide antes de tudo: em `manual` (o padrão) NADA consulta sozinho —
    o abastecimento serve só o que já está em cache, e quem gasta é um humano pelo botão
    do Histórico. Isto aqui só devolve True em `automatico`.

    `confirmada is False` = o consenso não fechou, e a própria docs/INTEGRACAO_ROTEADOR.md
    manda o roteador NÃO cobrar essa placa. Pagar para enriquecer um valor que ninguém
    deve usar é gasto certo por benefício nenhum — e pior: leitura não confirmada é
    candidata a estar ERRADA, ou a não ser placa alguma (ver o falso positivo sobre
    asfalto documentado em `app/visao/consenso.py`), então cada uma tende a virar um 406 e
    uma linha de cache negativo de uma placa que não existe.

    `is False` e não `not`: None é consenso DESCONHECIDO (origens que não passam pelo
    laço), e desconhecido não é o mesmo que fraco — mesma distinção que `_status_da_leitura`
    faz logo abaixo.

    Sair por timeout significa que o laço NUNCA fechou consenso, mesmo que `confirmada`
    não tenha vindo `False`. É o caso que consome os 28s inteiros — e justamente o que não
    vale pagar para enriquecer.

    Quem quiser o contrário (mostrar marca/modelo ao atendente na leitura duvidosa)
    desliga `apiplacas_exigir_confirmada`. Mesmo com ele ligado, a leitura não confirmada
    AINDA recebe consulta CACHE-ONLY: se a placa já foi paga alguma vez, o dado aparece
    de graça.
    """
    if not resultado.get("placa"):
        return False
    # O modo vem primeiro por ser o portão mais categórico: em `manual` NADA consulta
    # sozinho, e não faz sentido avaliar consenso para uma consulta que não vai acontecer.
    # A leitura continua entregando a placa normalmente — o que muda é só o `veiculo`, que
    # passa a vir do cache ou não vir.
    if (cfg.get("apiplacas_modo") or "manual").strip().lower() != "automatico":
        return False
    if not config.get_bool(cfg, "apiplacas_exigir_confirmada"):
        return True
    if resultado.get("confirmada") is False:
        return False
    return resultado.get("parada_motivo") != "timeout"


def bloco_veiculo(resultado: dict, cfg: dict, decorrido: float,
                  perfil: str = leitura.PERFIL_COMPLETO) -> dict | None:
    """O `veiculo{}` do payload, ou None quando não há o que acrescentar.

    Mora AQUI, e não dentro de `ler_placa`, por três motivos que se somam:

    - **custo**: `ler_placa` tem um segundo chamador, `bicos_ler_placa_teste`
      (`app/web/cadastro.py`), que é o botão "Testar como o roteador" e o editor de ROI.
      Gancho lá dentro faria cada clique de ajuste de enquadramento custar uma consulta
      paga — e ajuste de ROI é feito em rajada.
    - **correção**: dentro de `_ler_placa` a placa eleita ainda não é final —
      `_mesclar_com_historico` pode trocá-la depois, revotando contra a detecção anterior
      do mesmo bico. Consultar antes disso gravaria cache sob uma placa que a própria
      função descarta, e entregaria ao posto os dados de outro veículo.
    - **contrato**: `testes/unitarios/test_payload_leitura.py` congela as chaves de
      `ler_placa` de propósito. Esta camada já acrescenta chaves fora daquele contrato
      (entidade/cnpj/automacao/bico); `veiculo` pertence ao mesmo nível.

    Devolve None quando o recurso está desligado ou não há placa — nesses casos o payload
    sai byte a byte igual ao de antes desta feature.

    `except Exception` amplo: a leitura NUNCA pode quebrar por causa da API externa. Mesmo
    espírito do `_registrar` desta rota e do `_enviar_webhook` do pipeline.
    """
    if not config.get_bool(cfg, "apiplacas_ativo") or not resultado.get("placa"):
        return None
    try:
        # Perfil rápido é SEMPRE cache-only, sem orçamento de rede — mesmo com
        # `apiplacas_modo=automatico`. Duas razões independentes, e cada uma bastaria:
        # uma consulta externa custa até `apiplacas_timeout_seg` (2,5s hoje), que sozinha
        # é metade do orçamento inteiro do modo; e o modo rápido produz mais leitura não
        # confirmada, que é justamente a que `_pode_gastar` já se recusa a pagar. Placa
        # em cache continua vindo de graça e instantânea.
        if perfil == leitura.PERFIL_RAPIDO:
            return apiplacas.consultar(resultado["placa"], cfg,
                                       permitir_gasto=False, orcamento_seg=0.0)
        # O que sobrou do orçamento da leitura. A consulta externa não pode empurrar a
        # resposta para além do que o roteador tolera esperar.
        teto = config.get_float(cfg, "apiplacas_timeout_seg")
        folga = config.get_int(cfg, "leitura_timeout_seg") + teto - decorrido
        return apiplacas.consultar(resultado["placa"], cfg,
                                   permitir_gasto=_pode_gastar(resultado, cfg),
                                   orcamento_seg=max(0.0, min(teto, folga)))
    except Exception as e:
        log.error("Falha ao consultar dados do veículo: %s", e)
        return None


def _status_da_leitura(resultado: dict) -> tuple[str, str]:
    """Classifica o resultado de `ler_placa` para o log de chamadas: (status, motivo).

    Função separada porque esta regra decide o que conta como sucesso — e portanto o que
    o painel mostra como taxa de sucesso e o que um atendente trata como placa boa. Uma
    leitura que saiu por timeout devolve placa mas NÃO é sucesso: contá-la como 'ok'
    esconderia justamente as leituras que precisam de conferência antes de virar cobrança.

    `confirmada` ausente (None) significa consenso desconhecido, não consenso fraco — é o
    caso de chamadas antigas e de origens que não passam pelo loop; nesses, não rebaixa.
    """
    if not resultado.get("placa"):
        return "sem_placa", (resultado.get("mensagem") or "sem placa")
    acordo = resultado.get("acordo")
    acordo_txt = f"{acordo:.2f}" if isinstance(acordo, (int, float)) else "?"
    if resultado.get("confirmada") is False:
        # Relata os números em vez de afirmar a causa: `confirmada` cai por acordo baixo
        # OU por votos de menos (uma única foto detectando fecha acordo em 1.0 sozinha,
        # ver `_confirmada`). A mensagem antiga dizia sempre "acordo abaixo do mínimo" e
        # ficava simplesmente errada no segundo caso — "acordo 1.00 abaixo do mínimo".
        votos = resultado.get("votos_snapshot")
        total = resultado.get("total_snapshots")
        votos_txt = f", {votos}/{total} fotos" if votos is not None and total else ""
        return "nao_confirmada", (f"consenso insuficiente: acordo {acordo_txt}{votos_txt} "
                                  f"(parada: {resultado.get('parada_motivo')})")
    # Sair por timeout significa que o loop NUNCA fechou consenso: a parada por consenso
    # é o outro motivo possível (`parada_motivo == "acordo"`). A regra estava só na
    # docstring acima — o código olhava apenas `confirmada`, então uma leitura que
    # esgotou o tempo e voltou com a candidata menos ruim era contada como sucesso na
    # taxa do painel. `nao_confirmada` (e não um status novo) porque o significado é o
    # mesmo que o painel já mostra como "A conferir": devolveu placa, precisa de olho
    # humano antes de virar cobrança.
    # Timeout com `confirmada` TRUE deixou de rebaixar em 25/08/2026, e a mudanca e sutil o
    # bastante para merecer o porque inteiro.
    #
    # A regra nasceu correta: enquanto a confirmacao exigia 2 FOTOS, o laco so parava por
    # `acordo` quando fechava consenso de verdade, entao `timeout` significava mesmo "nunca
    # fechou" e a leitura era a candidata menos ruim, nao sucesso.
    #
    # O que mudou e que `confirmada` passou a contar LEITURAS (o ensemble da 3-4 por foto), e
    # com o GET conseguindo 1 foto em 28 s a parada por acordo ficou rara mesmo com evidencia
    # sobrando. `SKU7G13` saiu com acordo 100%, confianca 95% e 4 leituras concordantes - e
    # era rebaixada aqui por ter esgotado o tempo DEPOIS de ja ter decidido. Rebaixar isso
    # nao protege ninguem: esconde leitura boa atras de "a conferir" e, com
    # `apiplacas_exigir_confirmada`, impede a consulta de dados do veiculo para sempre.
    #
    # O que a regra defendia continua defendido: timeout SEM consenso segue rebaixando, e e
    # o caso que de fato precisa de olho humano.
    if resultado.get("parada_motivo") == "timeout" and not resultado.get("confirmada"):
        return "nao_confirmada", f"tempo esgotado sem consenso (acordo {acordo_txt})"
    return "ok", ""


# Campos de `ler_placa` que carregam link de imagem. Existem em DOIS níveis do payload:
# no topo (`snapshot`, `frame_url`) e dentro de cada item de `fontes[]` (`frame_url` da
# câmera daquela fonte). Ver `_sem_imagens`.
CHAVES_IMAGEM = ("snapshot", "frame_url")


def _sem_imagens(payload: dict) -> dict:
    """`payload` sem nenhum link de imagem — a forma que o integrador recebe.

    A foto da leitura é do POSTO: quem a vê é quem entra no sistema web (histórico, editor
    de ROI, tela do bico). O sidecar que consome `/api/leitura` recebe a placa e os números
    que a sustentam, não a imagem.

    Remove nos dois níveis, e o segundo é o que se esquece: tirar só o do topo deixaria o
    link exposto em `fontes[]` no bico de DUAS câmeras — justamente o caso com mais imagens
    no payload.

    **Não mexe em `ler_placa`**, de propósito. Os campos continuam saindo de lá, no contrato
    congelado por `testes/unitarios/test_payload_leitura.py`, porque o outro chamador é o
    botão "Testar como o roteador" do painel (`app/web/cadastro.py`), que PRECISA das
    imagens: é o que o operador olha para ajustar enquadramento. O corte é na entrega, não
    na produção.

    **Não mexe no que é gravado**: `deteccoes.snapshot` continua apontando para o arquivo,
    senão o histórico do painel perderia a foto de toda leitura nova.

    Copia em vez de `pop`: os dicts de `fontes` são criados dentro de `ler_placa`, e mutá-los
    daria a esta função efeito colateral sobre objeto que ela não criou.
    """
    limpo = {k: v for k, v in payload.items() if k not in CHAVES_IMAGEM}
    fontes = limpo.get("fontes")
    if isinstance(fontes, list):
        limpo["fontes"] = [
            {k: v for k, v in f.items() if k not in CHAVES_IMAGEM}
            if isinstance(f, dict) else f
            for f in fontes
        ]
    return limpo


def _sessao_autoriza(request: Request, empresa_id: int | None) -> bool:
    """O painel, logado, pode testar a leitura de um posto SEM mandar a api_key dele.

    `/api/leitura` é rota pública, então o middleware não resolve a sessão — daí a
    resolução manual aqui. Vale só para conferir o acesso: quem não tem sessão válida cai
    no caminho da chave, exatamente como antes.

    Existe porque a tela do posto tinha um botão de teste que mandava
    `X-API-Key: <chave do posto>` lida do JSON de `/api/empresas`. Com a chave redigida na
    resposta (achado K3), o botão passaria a mandar a MÁSCARA e receber 404 — o painel
    quebraria a própria ferramenta de diagnóstico. Autenticar por sessão é melhor que
    devolver o segredo ao navegador só para ele mandar de volta.
    (Revisão da auditoria de 27/08/2026.)
    """
    token = request.cookies.get("sessao")
    if not token:
        return False
    try:
        user_id = auth_mod.obter_user_id(token)
        if user_id is None:
            return False
        user = banco.buscar_usuario_id(user_id)
    except Exception:
        return False
    if not user or not user["ativo"]:
        return False
    if user.get("papel") == "admin":
        return True
    # 'cliente'/'operador' só testam o PRÓPRIO posto.
    return user.get("empresa_id") is not None and user["empresa_id"] == empresa_id


@router.get("/leitura")
def leitura_reativa(
    request: Request,
    entidade: str = Query(...),
    cnpj: str = Query(...),
    automacao: str = Query(...),
    bico: str = Query(...),
    rapido: bool = Query(False),
):
    inicio = time.time()
    cnpj_norm = re.sub(r"\D", "", cnpj)

    # Perfil resolvido ANTES de tudo: ele dimensiona a espera pelo primeiro frame, que
    # acontece na abertura das fontes, antes de qualquer orçamento de laço existir.
    cfg = config.carregar()
    perfil, aviso_perfil = perfil_pedido(rapido, cfg)

    base = {"entidade": entidade, "cnpj": cnpj_norm, "automacao": automacao, "bico": bico,
            "modo": perfil}

    def _registrar(status: str, motivo: str = "", **extra) -> None:
        # Nunca deixa o log derrubar a resposta ao roteador.
        try:
            banco.registrar_chamada(
                **base, status=status, motivo=motivo,
                duracao_ms=int((time.time() - inicio) * 1000), **extra,
            )
        except Exception as e:
            log.warning("Falha ao registrar chamada: %s", e)

    ip = request.client.host if request.client else "?"
    if not limitador.permitido("leitura_ip", ip, _LIMITE_LEITURA_IP_MIN, 60):
        _registrar("erro_cadastro", "rate limit por IP excedido")
        raise HTTPException(429, "Muitas requisições — tente novamente em instantes.")
    if not limitador.permitido("leitura_cnpj", cnpj_norm, _LIMITE_LEITURA_CNPJ_MIN, 60):
        _registrar("erro_cadastro", "rate limit por CNPJ excedido")
        raise HTTPException(429, "Muitas requisições para este CNPJ — tente novamente em instantes.")

    reg, motivo = banco.resolver_bico(cnpj_norm, automacao, bico)

    # A resposta de cadastro-não-encontrado é SEMPRE a mesma string, sem dizer em que nível
    # a resolução parou. Antes ela dizia 'empresa' / 'automacao' / 'bico', e como esta rota é
    # pública isso virava um oráculo de enumeração: três requisições mapeavam quais CNPJs são
    # clientes, quantas automações cada um tem e quantos bicos. Pior, o vazamento acontecia
    # ANTES da checagem de api_key logo abaixo, então valia até para posto com chave
    # configurada — o gate protegia só a última porta, e as duas anteriores já tinham contado
    # tudo. (Auditoria 27/08/2026, achado A2.)
    #
    # O DIAGNÓSTICO não se perde: o nível exato vai para `chamadas` via `_registrar` e para o
    # log do servidor, que é onde o integrador e o suporte olham. Quem está do lado de fora,
    # sem credencial, recebe só "não encontrado".
    _RESPOSTA_CADASTRO = "Cadastro não encontrado ou credencial inválida"

    if reg is None:
        # "_inativa"/"_inativo": o cadastro existe mas foi desativado — a correção é
        # diferente (reativar, não criar), e por isso a distinção continua no registro
        # interno, mesmo saindo igual na resposta.
        if motivo.endswith(("_inativa", "_inativo")):
            nivel = motivo.rsplit("_", 1)[0]
            detalhe = f"nível '{nivel}' está desativado no cadastro"
        else:
            nivel = motivo
            detalhe = f"não encontrado no nível '{nivel}'"
        _registrar("erro_cadastro", f"cadastro: {detalhe}")
        log.warning("ler-placa cnpj=%s automacao=%s bico=%s: %s",
                    cnpj_norm, automacao, bico, detalhe)
        raise HTTPException(404, _RESPOSTA_CADASTRO)
    base.update(bico_id=reg["bico_id"], empresa_id=reg["empresa_id"])

    # Chave própria do cliente (opt-in): só exige quando a empresa TEM uma api_key
    # cadastrada (app/core/banco.py:empresas_gerar_api_key) — postos sem chave própria
    # continuam públicos como sempre. 404 (não 401/403) para não revelar, a quem não
    # tem a chave, que o cadastro existe e só falta autenticação.
    chave_empresa = reg.get("empresa_api_key") or ""
    if chave_empresa and not _sessao_autoriza(request, reg["empresa_id"]):
        enviada = request.headers.get("X-API-Key", "") or request.query_params.get("api_key", "")
        # `compare_digest` e não `!=`: comparação de string sai no primeiro byte diferente e
        # vaza o prefixo correto pelo tempo. O projeto já usava o padrão certo para a chave
        # do posto no middleware (`app/servidor.py`); faltava aqui.
        if not secrets.compare_digest(enviada, chave_empresa):
            _registrar("erro_cadastro", "api_key do posto ausente ou incorreta")
            raise HTTPException(404, _RESPOSTA_CADASTRO)

    ent = banco.entidades_obter(reg["entidade_id"])
    if ent and ent["nome"].strip().lower() != entidade.strip().lower():
        # Não bloqueia — só sinaliza divergência entre o que o roteador enviou e o cadastro.
        log.warning("Entidade divergente: recebida=%r cadastrada=%r (cnpj=%s)",
                    entidade, ent["nome"], cnpj_norm)

    try:
        resultado = leitura.ler_placa(
            fontes=montar_fontes(reg["cameras"], cfg, perfil), cfg=cfg,
            avisos=reg.get("avisos"),
            preview_nome=f"preview_bico_{reg['bico_id']}", bico_id=reg["bico_id"],
            origem="roteador", perfil=perfil,
            # Só para o modo feira decidir se PODE mockar esta chamada — o mock vale
            # apenas no posto de demonstração (app/visao/feira.py).
            empresa_id=reg["empresa_id"],
        )
    except leitura.LeituraError as e:
        _registrar("erro_camera", e.mensagem)
        raise HTTPException(e.status, e.mensagem)

    status_chamada, motivo_chamada = _status_da_leitura(resultado)
    # Leitura degradada (uma das câmeras fora) que MESMO ASSIM devolveu placa boa continua
    # 'ok': ela produziu o resultado que o posto precisa, e rebaixá-la falsearia a taxa de
    # sucesso do painel. O aviso vai no motivo, que é onde o painel agrupa o que houve —
    # assim a câmera caída aparece para quem cuida da infraestrutura sem virar incidente
    # de leitura para quem cuida da cobrança.
    # `rapido=1` pedido com o modo desligado no servidor: a leitura rodou completa, e quem
    # integrou precisa saber disso — senão fica esperando em 5s uma chamada que
    # legitimamente leva 30. Entra no `avisos` do PAYLOAD (não só no motivo interno):
    # quem fez a pergunta é o chamador, e é ele que precisa da resposta. Não vira
    # `status` de erro — a leitura não falhou.
    if aviso_perfil:
        resultado.setdefault("avisos", []).append(aviso_perfil)
    avisos = resultado.get("avisos") or []
    if avisos:
        motivo_chamada = "; ".join([motivo_chamada, *avisos]) if motivo_chamada else "; ".join(avisos)
    veiculo = bloco_veiculo(resultado, cfg, time.time() - inicio, perfil)
    if veiculo is not None and veiculo["consulta"] == apiplacas.CONSULTA_INDISPONIVEL:
        # Vai para `motivo`, NUNCA para `status`: a placa foi lida bem, e rebaixar o
        # status falsearia a taxa de sucesso da leitura — mesmo raciocínio já aplicado
        # aos `avisos` acima. Mas precisa aparecer em ALGUM lugar que o operador já olha,
        # senão "o crédito da apiplacas acabou" só é descoberto quando um posto reclama
        # que o combustível parou de vir.
        aviso_veiculo = f"veiculo: {veiculo['motivo']}"
        motivo_chamada = "; ".join(filter(None, [motivo_chamada, aviso_veiculo]))

    _registrar(
        status_chamada, motivo_chamada,
        placa=resultado.get("placa"),
        acordo=resultado.get("acordo"),
        tentativas=resultado.get("tentativas"),
    )
    # `_sem_imagens` por último, sobre o payload já montado: assim vale para os DOIS
    # desfechos (com e sem placa) e para o que o `veiculo` porventura acrescente, sem
    # depender de lembrar de filtrar em cada ramo.
    return _sem_imagens({"entidade": entidade, "cnpj": cnpj_norm, "automacao": automacao,
                         "bico": bico, **resultado,
                         **({"veiculo": veiculo} if veiculo is not None else {})})
