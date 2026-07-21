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

from fastapi import APIRouter, HTTPException, Query

from app.core import banco
from app.core import config
from app.core import estado
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

    def _obter():
        # Na primeira chamada aguarda o pipeline aquecer. Sem isso a leitura desistiria
        # do pipeline e cairia numa conexão direta que não pode dar certo (câmera ocupada).
        limite = time.time() + (ESPERA_PRIMEIRO_FRAME_SEG if primeira[0] else 0)
        primeira[0] = False
        while True:
            idade = time.time() - estado.ultimo_frame_ts.get(camera_id, 0)
            if idade <= FRAME_MAX_IDADE_SEG:
                # frame LIMPO: o anotado tem bbox/label desenhados e o OCR leria o overlay
                f = estado.obter_frame_camera_limpo(camera_id)
                if f is None:
                    f = estado.obter_frame_camera(camera_id)
                if f is not None:
                    return f
            if time.time() >= limite:
                log.warning("frame_ao_vivo cam=%s: desistiu (idade do ultimo frame=%.1fs, "
                            "limpo=%s, anotado=%s)", camera_id, idade,
                            estado.obter_frame_camera_limpo(camera_id) is not None,
                            estado.obter_frame_camera(camera_id) is not None)
                return None
            time.sleep(0.2)

    return _obter


@router.get("/leitura")
def leitura_reativa(
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

    reg, motivo = banco.resolver_bico(cnpj_norm, automacao, bico)
    if reg is None:
        _registrar("erro_cadastro", f"cadastro não encontrado: {motivo}")
        raise HTTPException(
            404,
            f"Cadastro não encontrado no nível '{motivo}' "
            f"(cnpj={cnpj_norm} automacao={automacao} bico={bico})",
        )
    base.update(bico_id=reg["bico_id"], empresa_id=reg["empresa_id"])

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

    _registrar(
        "ok" if resultado.get("placa") else "sem_placa",
        "" if resultado.get("placa") else (resultado.get("mensagem") or "sem placa"),
        placa=resultado.get("placa"),
        acordo=resultado.get("acordo"),
        tentativas=resultado.get("tentativas"),
    )
    return {"entidade": entidade, "cnpj": cnpj_norm, "automacao": automacao,
            "bico": bico, **resultado}
