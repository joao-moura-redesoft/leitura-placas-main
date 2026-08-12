"""GET /api/leitura — endpoint reativo multi-tenant.

O roteador do posto (sidecar Java, fora do nosso escopo) chama este endpoint quando um
abastecimento TERMINA, passando entidade/cnpj/automacao/bico. Localizamos a câmera+ROI
do bico, tiramos uma foto fresca agora e devolvemos a placa lida — nada de pipeline
contínuo envolvido.
"""
from __future__ import annotations
import json
import logging
import re
import time

from fastapi import APIRouter, HTTPException, Query, Request

from app.core import banco
from app.core import config
from app.core import estado
from app.seguranca import limitador
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


def frame_ao_vivo(camera_id: int):
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
        limite = time.time() + (ESPERA_PRIMEIRO_FRAME_SEG if primeira[0] else 0)
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


# Limites do endpoint reativo — ele é PÚBLICO por design (rede interna do sidecar Java,
# ver app/servidor.py), então isto não é controle de acesso, é só um freio contra
# varredura/abuso (cnpj/automacao/bico errados de propósito, tentando descobrir cadastro
# válido). Generoso o bastante para não incomodar tráfego real: um posto reabastece bem
# menos que isso por minuto, mesmo com retries do roteador.
_LIMITE_LEITURA_IP_MIN = 60
_LIMITE_LEITURA_CNPJ_MIN = 30


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
    if resultado.get("confirmada") is False:
        acordo = resultado.get("acordo")
        acordo_txt = f"{acordo:.2f}" if isinstance(acordo, (int, float)) else "?"
        return "nao_confirmada", (f"acordo {acordo_txt} abaixo do mínimo "
                                  f"(parada: {resultado.get('parada_motivo')})")
    return "ok", ""


@router.get("/leitura")
def leitura_reativa(
    request: Request,
    entidade: str = Query(...),
    cnpj: str = Query(...),
    automacao: str = Query(...),
    bico: str = Query(...),
):
    inicio = time.time()
    cnpj_norm = re.sub(r"\D", "", cnpj)
    base = {"entidade": entidade, "cnpj": cnpj_norm, "automacao": automacao, "bico": bico}

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
    if reg is None:
        # "_inativa"/"_inativo": o cadastro existe mas foi desativado — mensagem
        # diferente de "não existe", porque a correção é diferente (reativar, não criar).
        if motivo.endswith(("_inativa", "_inativo")):
            nivel = motivo.rsplit("_", 1)[0]
            detalhe = f"nível '{nivel}' está desativado no cadastro"
        else:
            nivel = motivo
            detalhe = f"não encontrado no nível '{nivel}'"
        _registrar("erro_cadastro", f"cadastro: {detalhe}")
        raise HTTPException(
            404,
            f"Cadastro {detalhe} "
            f"(cnpj={cnpj_norm} automacao={automacao} bico={bico})",
        )
    base.update(bico_id=reg["bico_id"], empresa_id=reg["empresa_id"])

    # Chave própria do cliente (opt-in): só exige quando a empresa TEM uma api_key
    # cadastrada (app/core/banco.py:empresas_gerar_api_key) — postos sem chave própria
    # continuam públicos como sempre. 404 (não 401/403) para não revelar, a quem não
    # tem a chave, que o cadastro existe e só falta autenticação.
    chave_empresa = reg.get("empresa_api_key") or ""
    if chave_empresa:
        enviada = request.headers.get("X-API-Key", "") or request.query_params.get("api_key", "")
        if enviada != chave_empresa:
            _registrar("erro_cadastro", "api_key do posto ausente ou incorreta")
            raise HTTPException(
                404,
                f"Cadastro não encontrado no nível 'empresa' "
                f"(cnpj={cnpj_norm} automacao={automacao} bico={bico})",
            )

    ent = banco.entidades_obter(reg["entidade_id"])
    if ent and ent["nome"].strip().lower() != entidade.strip().lower():
        # Não bloqueia — só sinaliza divergência entre o que o roteador enviou e o cadastro.
        log.warning("Entidade divergente: recebida=%r cadastrada=%r (cnpj=%s)",
                    entidade, ent["nome"], cnpj_norm)

    cfg = config.carregar()
    especificacao = leitura.EspecificacaoCamera.de_camera_db(reg, cfg)
    roi = json.loads(reg["roi"]) if reg.get("roi") else None
    try:
        resultado = leitura.ler_placa(
            camera_id=reg["camera_id"], especificacao=especificacao, roi=roi, cfg=cfg,
            pipeline_frame_provider=frame_ao_vivo(reg["camera_id"]),
            preview_nome=f"preview_bico_{reg['bico_id']}", bico_id=reg["bico_id"],
            origem="roteador",
        )
    except leitura.LeituraError as e:
        _registrar("erro_camera", e.mensagem)
        raise HTTPException(e.status, e.mensagem)

    status_chamada, motivo_chamada = _status_da_leitura(resultado)
    _registrar(
        status_chamada, motivo_chamada,
        placa=resultado.get("placa"),
        acordo=resultado.get("acordo"),
        tentativas=resultado.get("tentativas"),
    )
    return {"entidade": entidade, "cnpj": cnpj_norm, "automacao": automacao,
            "bico": bico, **resultado}
