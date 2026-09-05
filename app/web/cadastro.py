"""CRUD administrativo do cadastro multi-tenant: entidades/empresas/automacoes/bicos.

Cadastro estrutural 100% manual da equipe RedSoft, sem replicação — criar/editar/apagar
entidade/posto/automação/bico/ROI continua admin-only (`Depends(deps.exigir_admin)` em
cada rota de escrita). O que MUDOU: usuários 'cliente' (app/web/usuarios.py), restritos a
UM posto, agora também acessam este router em modo leitura — as rotas de listagem/detalhe
filtram por `deps.empresa_do_usuario`/`deps.checar_acesso_empresa` para cada um só ver o
próprio posto. Mesmo padrão de validação/erros já usado no CRUD de câmeras (app/web/api.py).
"""
from __future__ import annotations
import json
import logging
import re
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core import banco
from app.core import config
from app.integracoes import apiplacas
from app.visao import leitura
from app.visao import feira
from app.visao import feira_fichas
from app.web import deps
from app.seguranca import limitador
from app.web import redacao
from app.web import leitura as leitura_rotas

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def _normalizar_cnpj(cnpj: str) -> str:
    return re.sub(r"\D", "", cnpj or "")


def _camera_id_opcional(valor) -> int | None:
    """`camera_id` vindo de corpo JSON: ausente/vazio = "não especificado"."""
    if valor in (None, "", 0):
        return None
    try:
        return int(valor)
    except (TypeError, ValueError):
        raise HTTPException(400, "camera_id deve ser inteiro")


def rois_faltando(bico: dict) -> list[int]:
    """Ids das câmeras deste bico que ainda não têm área desenhada.

    Um bico de duas câmeras com uma área só não está configurado pela metade em termos de
    resultado: na câmera sem área a leitura analisa o QUADRO INTEIRO, que é justamente o
    que o recorte existe para evitar. Por isso o checklist conta pares (bico, câmera).
    """
    faltando = []
    if not bico.get("roi"):
        faltando.append(bico["camera_id"])
    if bico.get("camera2_id") and not bico.get("roi2"):
        faltando.append(bico["camera2_id"])
    return faltando


def _cnpj_valido(cnpj: str) -> bool:
    """Dígito verificador padrão (módulo 11). Sem isso, `banco.py` aceitava qualquer
    sequência de 14 dígitos como CNPJ — inclusive erro de digitação óbvio (transposição
    de número), que só apareceria depois, quando o roteador do posto nunca resolvesse."""
    if len(cnpj) != 14 or cnpj == cnpj[0] * 14:
        return False

    def _dv(base: str, pesos: list[int]) -> str:
        soma = sum(int(d) * p for d, p in zip(base, pesos))
        resto = soma % 11
        return "0" if resto < 2 else str(11 - resto)

    d1 = _dv(cnpj[:12], [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    d2 = _dv(cnpj[:12] + d1, [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    return cnpj[-2:] == d1 + d2


def _integridade(e: sqlite3.IntegrityError, conflito: str) -> HTTPException:
    if "UNIQUE" in str(e):
        return HTTPException(409, conflito)
    return HTTPException(400, f"Referência inválida: {e}")


# ─── Visão consolidada por posto ─────────────────────────────────────────────
# O cadastro tem 4 níveis, mas o time trabalha por POSTO. Estes dois endpoints
# montam a visão inteira de uma vez para a tela não precisar cruzar 5 listagens.

@router.get("/postos")
def postos_listar(request: Request):
    entidades = {e["id"]: e for e in banco.entidades_listar()}
    automacoes = banco.automacoes_listar()
    bicos = banco.bicos_listar()
    escopo = deps.empresa_do_usuario(request)
    saida = []
    for emp in banco.empresas_listar():
        if escopo is not None and emp["id"] != escopo:
            continue
        autos = [a for a in automacoes if a["empresa_id"] == emp["id"]]
        ids = {a["id"] for a in autos}
        meus_bicos = [b for b in bicos if b["automacao_id"] in ids]
        cams = banco.cameras_listar(empresa_id=emp["id"])
        ent = entidades.get(emp["entidade_id"])
        saida.append({
            # `redacao.empresa` e não `**emp` cru: a api_key do posto saía aqui para
            # `cliente` e `operador` (auditoria 27/08, K3).
            **redacao.empresa(emp),
            "entidade_nome": ent["nome"] if ent else "",
            "n_automacoes": len(autos),
            "n_bicos": len(meus_bicos),
            "n_cameras": len(cams),
            # Conta BICOS com área faltando (é o que a tela lista como pendência), mas a
            # pendência de um bico de 2 câmeras inclui a área da segunda.
            "n_bicos_sem_roi": sum(1 for b in meus_bicos if rois_faltando(b)),
            # "pronto" = dá para o roteador chamar e obter leitura útil
            "pronto": bool(cams and autos and meus_bicos
                           and not any(rois_faltando(b) for b in meus_bicos)),
        })
    saida.sort(key=lambda p: (p["entidade_nome"], p["nome"]))
    return saida


@router.get("/postos/{empresa_id}")
def posto_detalhe(empresa_id: int, request: Request):
    deps.checar_acesso_empresa(request, empresa_id)
    emp = banco.empresas_obter(empresa_id)
    if not emp:
        raise HTTPException(404, "Posto não encontrado")
    ent = banco.entidades_obter(emp["entidade_id"])
    cams = {c["id"]: c for c in banco.cameras_listar(empresa_id=empresa_id)}
    # `ao_vivo` = há pipeline contínuo ENTREGANDO frames → a tela pode exibir MJPEG
    # em vez de captura sob demanda (ver `pipeline.estado_stream`).
    from app.visao import pipeline as pipeline_mod
    for c in cams.values():
        c["stream_modo"] = pipeline_mod.estado_stream(c["id"])
        c["ao_vivo"] = c["stream_modo"] == "ao_vivo"
    autos = []
    for a in banco.automacoes_listar(empresa_id=empresa_id):
        bicos = []
        for b in banco.bicos_listar(automacao_id=a["id"]):
            cam = cams.get(b["camera_id"]) or banco.cameras_obter(b["camera_id"]) or {}
            faltando = rois_faltando(b)
            # `cameras` descreve os slots do bico de uma vez; os campos avulsos
            # (`camera_nome`, `tem_roi`) continuam apontando para a primeira câmera para
            # as telas que ainda não leem a lista.
            fontes = []
            for slot, camera_id, roi, papel in banco.slots_do_bico(b):
                c = cams.get(camera_id) or banco.cameras_obter(camera_id) or {}
                fontes.append({"camera_id": camera_id, "papel": papel,
                               "nome": c.get("nome", "?"), "local": c.get("local", ""),
                               "tem_roi": bool(roi), "slot": slot})
            bicos.append({**b,
                          "camera_nome": cam.get("nome", "?"),
                          "camera_local": cam.get("local", ""),
                          "cameras": fontes,
                          "rois_faltando": faltando,
                          "tem_roi": bool(b["roi"])})
        autos.append({**a, "bicos": bicos})
    return {
        "empresa": redacao.empresa(emp),
        "entidade": ent,
        # `cams` foi mutado acima com `stream_modo`; a redação copia, então o dict de
        # trabalho continua intacto para quem ainda o usa nesta request.
        "cameras": redacao.cameras(cams.values()),
        "automacoes": autos,
    }


# ─── Entidades ───────────────────────────────────────────────────────────────

@router.get("/entidades")
def entidades_listar(request: Request):
    escopo = deps.empresa_do_usuario(request)
    if escopo is None:
        return banco.entidades_listar()
    # Cliente só enxerga a rede (entidade) dona do próprio posto.
    emp = banco.empresas_obter(escopo)
    if not emp:
        return []
    ent = banco.entidades_obter(emp["entidade_id"])
    return [ent] if ent else []


@router.post("/entidades", dependencies=[Depends(deps.exigir_admin)])
def entidades_inserir(payload: dict, request: Request):
    nome = (payload.get("nome") or "").strip()
    if not nome:
        raise HTTPException(400, "nome é obrigatório")
    id_ = banco.entidades_inserir({**payload, "nome": nome})
    quem_id, quem_nome = deps.quem_pede(request)
    banco.auditoria_registrar(usuario_id=quem_id, usuario_nome=quem_nome, acao="entidade_criada",
                              alvo_tipo="entidade", alvo_id=id_, detalhe=f"nome={nome}")
    return {"id": id_}


@router.put("/entidades/{id_}", dependencies=[Depends(deps.exigir_admin)])
def entidades_atualizar(id_: int, payload: dict, request: Request):
    nome = (payload.get("nome") or "").strip()
    if not nome:
        raise HTTPException(400, "nome é obrigatório")
    if not banco.entidades_atualizar(id_, {**payload, "nome": nome}):
        raise HTTPException(404, "Entidade não encontrada")
    quem_id, quem_nome = deps.quem_pede(request)
    banco.auditoria_registrar(usuario_id=quem_id, usuario_nome=quem_nome, acao="entidade_atualizada",
                              alvo_tipo="entidade", alvo_id=id_, detalhe=f"nome={nome}")
    return {"atualizado": True}


@router.delete("/entidades/{id_}", dependencies=[Depends(deps.exigir_admin)])
def entidades_remover(id_: int, request: Request):
    entidade = banco.entidades_obter(id_)
    if not banco.entidades_remover(id_):
        raise HTTPException(404, "Entidade não encontrada")
    quem_id, quem_nome = deps.quem_pede(request)
    banco.auditoria_registrar(
        usuario_id=quem_id, usuario_nome=quem_nome, acao="entidade_removida",
        alvo_tipo="entidade", alvo_id=id_,
        detalhe=f"nome={entidade['nome'] if entidade else '?'} (apaga postos/cameras/automacoes/bicos em cascata)",
    )
    return {"removido": True}


# ─── Empresas (CNPJ = 1 posto físico) ───────────────────────────────────────

@router.get("/empresas")
def empresas_listar(request: Request, entidade_id: int | None = None):
    escopo = deps.empresa_do_usuario(request)
    if escopo is None:
        return redacao.empresas(banco.empresas_listar(entidade_id=entidade_id))
    emp = banco.empresas_obter(escopo)
    if not emp or (entidade_id is not None and emp["entidade_id"] != entidade_id):
        return []
    return redacao.empresas([emp])


@router.get("/empresas/{id_}")
def empresas_obter(id_: int, request: Request):
    deps.checar_acesso_empresa(request, id_)
    emp = banco.empresas_obter(id_)
    if not emp:
        raise HTTPException(404, "Empresa não encontrada")
    return redacao.empresa(emp)


@router.post("/empresas/{id_}/api-key", dependencies=[Depends(deps.exigir_admin)])
def empresas_gerar_api_key(id_: int, request: Request):
    """Gera (ou substitui) a api_key própria deste posto — opt-in: a partir daqui
    `/api/leitura` passa a exigir essa chave nas chamadas com este CNPJ. Ver
    app/web/leitura.py:leitura_reativa."""
    chave = banco.empresas_gerar_api_key(id_)
    if chave is None:
        raise HTTPException(404, "Empresa não encontrada")
    quem_id, quem_nome = deps.quem_pede(request)
    # A CHAVE em si nunca vai pra auditoria (é segredo) — só o fato de que foi gerada.
    banco.auditoria_registrar(usuario_id=quem_id, usuario_nome=quem_nome, acao="api_key_gerada",
                              alvo_tipo="empresa", alvo_id=id_)
    return {"api_key": chave}


@router.delete("/empresas/{id_}/api-key", dependencies=[Depends(deps.exigir_admin)])
def empresas_revogar_api_key(id_: int, request: Request):
    """Volta o posto ao padrão público (sem chave própria)."""
    if not banco.empresas_revogar_api_key(id_):
        raise HTTPException(404, "Empresa não encontrada")
    quem_id, quem_nome = deps.quem_pede(request)
    banco.auditoria_registrar(usuario_id=quem_id, usuario_nome=quem_nome, acao="api_key_revogada",
                              alvo_tipo="empresa", alvo_id=id_)
    return {"revogado": True}


@router.put("/empresas/{id_}/retencao", dependencies=[Depends(deps.exigir_admin)])
def empresas_definir_retencao(id_: int, payload: dict, request: Request):
    """Prazo de retenção próprio (LGPD por cliente) — `dias=null`/ausente volta a usar
    o `retencao_dias` global. Ver app/operacao/retencao.py."""
    dias = payload.get("dias")
    if dias is not None:
        try:
            dias = int(dias)
        except (TypeError, ValueError):
            raise HTTPException(400, "dias deve ser um número inteiro (ou null)")
        if dias < 0:
            raise HTTPException(400, "dias não pode ser negativo")
    if not banco.empresas_definir_retencao(id_, dias):
        raise HTTPException(404, "Empresa não encontrada")
    quem_id, quem_nome = deps.quem_pede(request)
    banco.auditoria_registrar(usuario_id=quem_id, usuario_nome=quem_nome, acao="retencao_definida",
                              alvo_tipo="empresa", alvo_id=id_, detalhe=f"dias={dias}")
    return {"retencao_dias_override": dias}


@router.post("/empresas", dependencies=[Depends(deps.exigir_admin)])
def empresas_inserir(payload: dict, request: Request):
    nome = (payload.get("nome") or "").strip()
    cnpj = _normalizar_cnpj(payload.get("cnpj", ""))
    if not nome or not cnpj:
        raise HTTPException(400, "nome e cnpj são obrigatórios")
    if not _cnpj_valido(cnpj):
        raise HTTPException(400, f"CNPJ {cnpj} inválido (dígito verificador não confere)")
    if not payload.get("entidade_id"):
        raise HTTPException(400, "entidade_id é obrigatório")
    try:
        id_ = banco.empresas_inserir({**payload, "nome": nome, "cnpj": cnpj})
    except sqlite3.IntegrityError as e:
        raise _integridade(e, f"CNPJ {cnpj} já cadastrado")
    quem_id, quem_nome = deps.quem_pede(request)
    banco.auditoria_registrar(usuario_id=quem_id, usuario_nome=quem_nome, acao="posto_criado",
                              alvo_tipo="empresa", alvo_id=id_, detalhe=f"nome={nome} cnpj={cnpj}")
    return {"id": id_}


@router.put("/empresas/{id_}", dependencies=[Depends(deps.exigir_admin)])
def empresas_atualizar(id_: int, payload: dict, request: Request):
    nome = (payload.get("nome") or "").strip()
    cnpj = _normalizar_cnpj(payload.get("cnpj", ""))
    if not nome or not cnpj:
        raise HTTPException(400, "nome e cnpj são obrigatórios")
    if not _cnpj_valido(cnpj):
        raise HTTPException(400, f"CNPJ {cnpj} inválido (dígito verificador não confere)")
    if not payload.get("entidade_id"):
        raise HTTPException(400, "entidade_id é obrigatório")
    try:
        ok = banco.empresas_atualizar(id_, {**payload, "nome": nome, "cnpj": cnpj})
    except sqlite3.IntegrityError as e:
        raise _integridade(e, f"CNPJ {cnpj} já cadastrado")
    if not ok:
        raise HTTPException(404, "Empresa não encontrada")
    quem_id, quem_nome = deps.quem_pede(request)
    banco.auditoria_registrar(usuario_id=quem_id, usuario_nome=quem_nome, acao="posto_atualizado",
                              alvo_tipo="empresa", alvo_id=id_, detalhe=f"nome={nome} cnpj={cnpj}")
    return {"atualizado": True}


@router.get("/empresas/{id_}/impacto-remocao")
def empresas_impacto_remocao(id_: int, request: Request):
    """O que se perde ao apagar este posto — para a tela poder AVISAR antes de perguntar.

    Um "tem certeza?" genérico não informa nada: a remoção desce em cascata por
    automações, bicos (com as áreas desenhadas) e câmeras, e ainda desativa os usuários
    'cliente' presos a este posto. Quem confirma precisa ver os números.
    """
    deps.exigir_admin(request)
    emp = banco.empresas_obter(id_)
    if not emp:
        raise HTTPException(404, "Posto não encontrado")
    autos = banco.automacoes_listar(empresa_id=id_)
    ids = {a["id"] for a in autos}
    bicos = [b for b in banco.bicos_listar() if b["automacao_id"] in ids]
    cfg = config.carregar()
    return {
        "nome": emp["nome"], "cnpj": emp["cnpj"],
        "cameras": len(banco.cameras_listar(empresa_id=id_)),
        "automacoes": len(autos),
        "bicos": len(bicos),
        "areas": sum(1 for b in bicos if b.get("roi") or b.get("roi2")),
        # `usuarios_listar` nao filtra por posto — o filtro e aqui. Sao os usuarios
        # 'cliente' presos a ESTE posto: `_apagar_empresas` os DESATIVA (nao apaga), e
        # quem confirma a remocao precisa saber que vai deixar gente sem acesso.
        "usuarios_cliente": sum(1 for u in banco.usuarios_listar()
                                if u.get("papel") == "cliente" and u.get("empresa_id") == id_),
        # Apagar o posto de demonstração tem de DESARMAR o modo feira junto.
        "e_posto_de_demonstracao": (cfg.get("feira_empresa_id") or "").strip() == str(id_),
    }


@router.delete("/empresas/{id_}", dependencies=[Depends(deps.exigir_admin)])
def empresas_remover(id_: int, request: Request):
    empresa = banco.empresas_obter(id_)
    if not banco.empresas_remover(id_):
        raise HTTPException(404, "Empresa não encontrada")

    # Se era o posto de demonstração, DESARMA o modo feira. Sem isto `feira_empresa_id`
    # fica apontando para um id que não existe mais: a tela do modo feira diria "não
    # criado" com a chave preenchida, e recriar o posto deixaria duas verdades em
    # disputa. (`empresas.id` é AUTOINCREMENT, então o id não é reaproveitado e o mock
    # não passaria a mirar outro posto — mas config morta é armadilha para a próxima
    # pessoa que ler o arquivo.)
    cfg = config.carregar()
    if (cfg.get("feira_empresa_id") or "").strip() == str(id_):
        cfg["feira_empresa_id"] = ""
        config.salvar(cfg)
        log.warning("Posto de demonstracao %s removido, modo feira DESARMADO.", id_)

    quem_id, quem_nome = deps.quem_pede(request)
    banco.auditoria_registrar(
        usuario_id=quem_id, usuario_nome=quem_nome, acao="posto_removido",
        alvo_tipo="empresa", alvo_id=id_,
        detalhe=f"nome={empresa['nome'] if empresa else '?'} cnpj={empresa['cnpj'] if empresa else '?'} "
                f"(apaga câmeras/automações/bicos em cascata)",
    )
    return {"removido": True}


# ─── Automações (até 2 por posto) ───────────────────────────────────────────

@router.get("/automacoes")
def automacoes_listar(request: Request, empresa_id: int | None = None):
    escopo = deps.empresa_do_usuario(request)
    if escopo is not None:
        if empresa_id is not None and empresa_id != escopo:
            return []
        empresa_id = escopo
    return banco.automacoes_listar(empresa_id=empresa_id)


@router.post("/automacoes", dependencies=[Depends(deps.exigir_admin)])
def automacoes_inserir(payload: dict):
    codigo = (payload.get("codigo") or "").strip()
    if not codigo:
        raise HTTPException(400, "codigo é obrigatório")
    if not payload.get("empresa_id"):
        raise HTTPException(400, "empresa_id é obrigatório")
    try:
        id_ = banco.automacoes_inserir({**payload, "codigo": codigo})
    except sqlite3.IntegrityError as e:
        raise _integridade(e, f"Código '{codigo}' já cadastrado para esta empresa")
    return {"id": id_}


@router.put("/automacoes/{id_}", dependencies=[Depends(deps.exigir_admin)])
def automacoes_atualizar(id_: int, payload: dict):
    codigo = (payload.get("codigo") or "").strip()
    if not codigo:
        raise HTTPException(400, "codigo é obrigatório")
    if not payload.get("empresa_id"):
        raise HTTPException(400, "empresa_id é obrigatório")
    try:
        ok = banco.automacoes_atualizar(id_, {**payload, "codigo": codigo})
    except sqlite3.IntegrityError as e:
        raise _integridade(e, f"Código '{codigo}' já cadastrado para esta empresa")
    if not ok:
        raise HTTPException(404, "Automação não encontrada")
    return {"atualizado": True}


@router.delete("/automacoes/{id_}", dependencies=[Depends(deps.exigir_admin)])
def automacoes_remover(id_: int):
    if not banco.automacoes_remover(id_):
        raise HTTPException(404, "Automação não encontrada")
    return {"removido": True}


# ─── Bicos ───────────────────────────────────────────────────────────────────

def _empresa_do_bico(bico: dict) -> int | None:
    automacao = banco.automacoes_obter(bico["automacao_id"])
    return automacao["empresa_id"] if automacao else None


@router.get("/bicos")
def bicos_listar(request: Request, automacao_id: int | None = None, camera_id: int | None = None):
    bicos = banco.bicos_listar(automacao_id=automacao_id, camera_id=camera_id)
    escopo = deps.empresa_do_usuario(request)
    if escopo is None:
        return bicos
    return [b for b in bicos if _empresa_do_bico(b) == escopo]


@router.get("/bicos/{id_}")
def bicos_obter(id_: int, request: Request):
    bico = banco.bicos_obter(id_)
    if not bico:
        raise HTTPException(404, "Bico não encontrado")
    deps.checar_acesso_empresa(request, _empresa_do_bico(bico))
    return bico


PAPEIS_CAMERA = ("traseira", "frente")


def _validar_camera_do_posto(camera_id: int, automacao: dict) -> dict:
    """Carrega a câmera e garante que ela pertence ao MESMO posto do bico.

    Sem isso, num servidor central seria possível apontar o bico do Posto A para a câmera
    do Posto B — o roteador de um cliente receberia a imagem do pátio de outro.
    """
    camera = banco.cameras_obter(camera_id)
    if not camera:
        raise HTTPException(400, "Câmera não encontrada")
    if camera.get("empresa_id") != automacao["empresa_id"]:
        emp_cam = banco.empresas_obter(camera["empresa_id"]) if camera.get("empresa_id") else None
        emp_bico = banco.empresas_obter(automacao["empresa_id"])
        raise HTTPException(
            400,
            f"A câmera '{camera['nome']}' pertence a "
            f"{emp_cam['nome'] if emp_cam else 'nenhuma empresa'} e o bico a "
            f"{emp_bico['nome'] if emp_bico else '?'} — escolha uma câmera do mesmo posto.",
        )
    return camera


def _validar_bico(payload: dict) -> None:
    """Valida as câmeras do bico (1 obrigatória + 1 opcional) e seus papéis.

    A segunda câmera passa pela MESMA checagem de posto da primeira: ela é uma fonte de
    imagem como qualquer outra, e deixá-la de fora reabriria por outro campo exatamente
    o vazamento entre postos que a validação da primeira fecha.
    """
    if not payload.get("automacao_id") or not payload.get("camera_id"):
        raise HTTPException(400, "automacao_id e camera_id são obrigatórios")

    automacao = banco.automacoes_obter(deps.inteiro_ou_400(payload["automacao_id"], "automacao_id"))
    if not automacao:
        raise HTTPException(400, "Automação não encontrada")

    camera_id = deps.inteiro_ou_400(payload['camera_id'], 'camera_id')
    _validar_camera_do_posto(camera_id, automacao)

    # Normaliza os papéis in-place (o payload segue daqui direto para o banco), inclusive
    # no bico de uma câmera — senão 'TRASEIRA' e 'traseira' viram rótulos diferentes na
    # tela e nos filtros do histórico.
    for campo, padrao in (("papel_camera", "traseira"), ("papel_camera2", "frente")):
        papel = (payload.get(campo) or padrao).strip().lower()
        if papel not in PAPEIS_CAMERA:
            raise HTTPException(400, f"{campo} deve ser um de: {', '.join(PAPEIS_CAMERA)}")
        payload[campo] = papel

    bruto2 = payload.get("camera2_id")
    if bruto2 in (None, "", 0):
        return                      # bico de uma câmera — nada mais a validar

    camera2_id = int(bruto2)
    if camera2_id == camera_id:
        raise HTTPException(
            400, "A segunda câmera precisa ser diferente da primeira: o ganho vem de "
                 "enxergar o veículo por outro ângulo.")
    _validar_camera_do_posto(camera2_id, automacao)

    # Papéis iguais nas duas câmeras não são só redundância de rótulo: o papel é o NOME
    # pelo qual a tela distingue as duas fontes (botão "Ler bico X (frente)", aviso
    # "⚠ frente: não detectou placa", quadro do teste). Com as duas chamadas "traseira" o
    # operador vê dois rótulos idênticos e não tem como saber em qual câmera mexer — que é
    # a única coisa que o diagnóstico de duas fontes existe para dizer.
    if payload["papel_camera"] == payload["papel_camera2"]:
        raise HTTPException(
            400, "As duas câmeras não podem enxergar o mesmo lado do veículo: uma é a "
                 "traseira e a outra a frente. É por esse nome que o diagnóstico da "
                 "leitura diz qual das duas precisa de ajuste.")


@router.post("/bicos", dependencies=[Depends(deps.exigir_admin)])
def bicos_inserir(payload: dict):
    codigo = (payload.get("codigo") or "").strip()
    if not codigo:
        raise HTTPException(400, "codigo é obrigatório")
    _validar_bico(payload)
    try:
        id_ = banco.bicos_inserir({**payload, "codigo": codigo})
    except sqlite3.IntegrityError as e:
        raise _integridade(e, f"Código '{codigo}' já cadastrado para esta automação")
    return {"id": id_}


@router.put("/bicos/{id_}", dependencies=[Depends(deps.exigir_admin)])
def bicos_atualizar(id_: int, payload: dict):
    codigo = (payload.get("codigo") or "").strip()
    if not codigo:
        raise HTTPException(400, "codigo é obrigatório")
    _validar_bico(payload)
    try:
        ok = banco.bicos_atualizar(id_, {**payload, "codigo": codigo})
    except sqlite3.IntegrityError as e:
        raise _integridade(e, f"Código '{codigo}' já cadastrado para esta automação")
    if not ok:
        raise HTTPException(404, "Bico não encontrado")
    return {"atualizado": True}


@router.delete("/bicos/{id_}", dependencies=[Depends(deps.exigir_admin)])
def bicos_remover(id_: int):
    if not banco.bicos_remover(id_):
        raise HTTPException(404, "Bico não encontrado")
    return {"removido": True}


def _slot_da_camera(bico: dict, camera_id: int | None) -> int:
    """Descobre a qual câmera do bico um ROI se refere.

    `camera_id` ausente = slot 1, que é o comportamento de sempre — todo chamador antigo
    continua correto sem mudar nada. Presente, tem que bater com uma das câmeras do bico:
    o retângulo só faz sentido nas coordenadas de uma câmera específica, então adivinhar
    aqui gravaria a área certa no lugar errado, sem erro nenhum na hora.
    """
    if camera_id is None:
        return 1
    if camera_id == bico["camera_id"]:
        return 1
    if bico.get("camera2_id") and camera_id == bico["camera2_id"]:
        return 2
    validas = [str(bico["camera_id"])] + ([str(bico["camera2_id"])] if bico.get("camera2_id") else [])
    raise HTTPException(
        400, f"A câmera {camera_id} não é deste bico — câmeras válidas: {', '.join(validas)}")


@router.put("/bicos/{id_}/roi", dependencies=[Depends(deps.exigir_admin)])
def bicos_salvar_roi(id_: int, payload: dict):
    """Salva a área de captura (ROI) do bico numa das câmeras dele.

    Bicos que compartilham câmera mantêm ROIs independentes — a leitura reativa recorta
    pela área do bico chamado no GET. `camera_id` no corpo escolhe a câmera quando o bico
    tem duas; omitido, vale a primeira.
    """
    try:
        x, y, w, h = int(payload["x"]), int(payload["y"]), int(payload["w"]), int(payload["h"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "x, y, w, h são obrigatórios e devem ser inteiros")
    if w <= 0 or h <= 0:
        raise HTTPException(400, "w e h devem ser positivos")
    bico = banco.bicos_obter(id_)
    if not bico:
        raise HTTPException(404, "Bico não encontrado")
    slot = _slot_da_camera(bico, _camera_id_opcional(payload.get("camera_id")))
    banco.bico_salvar_roi(id_, {"x": x, "y": y, "w": w, "h": h}, slot=slot)
    return {"salvo": True}


@router.delete("/bicos/{id_}/roi", dependencies=[Depends(deps.exigir_admin)])
def bicos_limpar_roi(id_: int, camera_id: int | None = None):
    """Remove a área de captura — leitura reativa desse bico volta a usar o frame completo."""
    bico = banco.bicos_obter(id_)
    if not bico:
        raise HTTPException(404, "Bico não encontrado")
    banco.bico_limpar_roi(id_, slot=_slot_da_camera(bico, camera_id))
    return {"limpo": True}


# Leituras de teste por minuto, por IP. A rota roda `leitura_timeout_seg` (28 s por padrão)
# segurando uma thread do pool e o lock da câmera. Sem freio, 40 chamadas paralelas
# esgotavam o threadpool e paravam o servidor inteiro por ~28 s — inclusive `/api/leitura`
# dos outros postos e o `/login`. Custo do ataque: 40 requisições, de qualquer sessão
# válida. (Auditoria 27/08/2026, achado K5.)
_LIMITE_LER_PLACA_MIN = 6


@router.post("/bicos/{id_}/ler-placa-teste")
def bicos_ler_placa_teste(id_: int, request: Request, rapido: bool = False):
    """Testa a leitura de um bico direto (sem montar a URL completa de
    entidade+cnpj+automacao+bico) — usado pelo editor de ROI (admin) e pela tela do
    posto (também liberado a 'cliente', escopado ao próprio posto — é diagnóstico
    read-only, não mexe em cadastro).

    Passa pelo mesmo gate de `ativo` (entidade/empresa/automação/bico/câmera) que a
    leitura reativa de verdade — senão o botão de teste do painel responde mesmo para
    um cadastro desativado, driblando a trava aplicada em produção.
    """
    # Freio ANTES de qualquer trabalho: esta rota segura uma thread do pool por até
    # `leitura_timeout_seg` e o lock da câmera junto.
    ip = request.client.host if request.client else "?"
    if not limitador.permitido("ler_placa_teste", ip, _LIMITE_LER_PLACA_MIN, 60):
        raise HTTPException(
            429, "Muitas leituras de teste seguidas. Aguarde um instante.")

    bico, motivo = banco.bico_verificar_ativo(id_)
    if bico is None:
        if motivo in ("bico", "empresa", "automacao"):
            raise HTTPException(404, "Bico não encontrado")
        nivel = motivo.rsplit("_", 1)[0]
        raise HTTPException(409, f"Nível '{nivel}' está desativado no cadastro")
    empresa_do_bico = _empresa_do_bico(bico)
    deps.checar_acesso_empresa(request, empresa_do_bico)
    fontes_db, avisos, falha = banco.cameras_do_bico(bico)
    if falha is not None:
        raise HTTPException(404, "Câmera do bico não encontrada")

    cfg = config.carregar()
    # `rapido` passa pela MESMA resolução do endpoint do roteador (inclusive o
    # `rapido_ativo=nao`): o botão existe para exercitar o que o roteador vai receber, e
    # um caminho próprio aqui já significaria testar outra coisa.
    perfil, aviso_perfil = leitura_rotas.perfil_pedido(rapido, cfg)
    try:
        resultado = leitura.ler_placa(
            fontes=leitura_rotas.montar_fontes(fontes_db, cfg, perfil),
            cfg=cfg, avisos=avisos,
            preview_nome=f"preview_bico_{id_}", bico_id=id_, origem="teste",
            perfil=perfil,
            # O botão do painel exercita o mesmo caminho do roteador, e isso inclui o
            # modo feira: sem o posto aqui, a demo funcionaria pelo GET e não pelo botão.
            empresa_id=empresa_do_bico,
        )
    except leitura.LeituraError as e:
        raise HTTPException(e.status, e.mensagem)
    if aviso_perfil:
        resultado.setdefault("avisos", []).append(aviso_perfil)

    # Modo feira: mesmo bloco que o GET do roteador vai devolver, montado da ficha local.
    # Este botão é o ÚNICO jeito de conferir a demo antes do evento sem um roteador na mão
    # — se ele mostrasse um `veiculo` diferente do que o posto recebe, a conferência
    # deixaria de valer justamente para o payload novo. Mesmo princípio já aplicado ao
    # `rapido` e ao `empresa_id` acima: aqui não pode existir caminho próprio.
    demo = feira_fichas.bloco_de_leitura(resultado)
    if demo is not None:
        resultado["veiculo"] = demo
        return resultado

    # Dados do veículo em modo CACHE-ONLY: este fluxo é o botão "Testar como o roteador" e
    # o editor de ROI, clicados em rajada enquanto se ajusta o enquadramento. Cada consulta
    # à apiplacas custa crédito pré-pago, então ajustar câmera não pode gastar dinheiro.
    # Mostrar o bloco mesmo assim é melhor que omiti-lo: quem está ajustando vê exatamente
    # o que o roteador vai receber, e quando o dado falta vem o motivo em vez de silêncio.
    if config.get_bool(cfg, "apiplacas_ativo") and resultado.get("placa"):
        try:
            resultado["veiculo"] = apiplacas.consultar(
                resultado["placa"], cfg, permitir_gasto=False)
        except Exception as e:
            log.error("Falha ao ler cache de veículo no teste do bico %s: %s", id_, e)
    return resultado


# ─── Posto de demonstração (modo feira) ──────────────────────────────────────
# Monta de uma vez a árvore que a leitura reativa exige — entidade → posto → câmera →
# automação → bico → ROI — apontada para uma webcam USB ou uma câmera da rede local.
# Sem isto, demonstrar num estande custa seis telas de cadastro manual antes de conseguir
# a primeira leitura.
#
# Endpoint no BACKEND, e não encadeando as rotas existentes no JS (como `posto_novo.html`
# faz): aqui a criação é idempotente e testável, e uma falha no meio não deixa metade da
# árvore órfã no banco de quem está montando o estande com o cliente esperando.

FEIRA_ENTIDADE = "DEMONSTRAÇÃO"
# CNPJ fixo, todo nove: passa no dígito verificador de `_cnpj_valido` (obrigatório) e é
# visualmente inconfundível como dado falso. Distinto do `11222333000181` usado pelos
# testes, para ninguém confundir cadastro de demonstração com fixture de teste.
FEIRA_CNPJ = "99999999000191"
FEIRA_POSTO = "Posto de Demonstração"
FEIRA_CODIGO = "1"          # automação e bico — a URL da demo fica curta


def _feira_entidade_id() -> int:
    """Entidade da demonstração, criando só se ainda não existe.

    `entidades` não tem UNIQUE no esquema, então sem esta busca cada clique no botão
    criaria uma entidade nova e a tela de postos encheria de "DEMONSTRAÇÃO" repetidas.
    """
    for e in banco.entidades_listar():
        if e["nome"] == FEIRA_ENTIDADE:
            return e["id"]
    return banco.entidades_inserir({"nome": FEIRA_ENTIDADE})


@router.post("/feira/posto", dependencies=[Depends(deps.exigir_admin)])
def feira_criar_posto(payload: dict, request: Request):
    """Cria (ou reaproveita) o posto de demonstração e ARMA o modo feira nele.

    O corpo descreve só a CÂMERA, com os mesmos campos de `POST /api/cameras`:
      USB  -> {"camera_tipo": "usb", "camera_indice": "0"}
      rede -> {"camera_tipo": "rtsp", "intelbras_host": "...", "intelbras_usuario": ...}

    Idempotente: chamado duas vezes, reaproveita o que já existe em vez de devolver 409.
    `empresas.cnpj` é UNIQUE global, então sem isso o segundo clique quebraria.
    """
    from app.visao import camera as camera_mod
    from app.visao import pipeline as pipeline_mod
    from app.web import api as api_rotas

    cfg = config.carregar()
    tipo = (payload.get("camera_tipo") or "usb").strip()

    entidade_id = _feira_entidade_id()
    empresa = banco.empresas_obter_por_cnpj(FEIRA_CNPJ)
    if empresa is None:
        empresa_id = banco.empresas_inserir(
            {"entidade_id": entidade_id, "cnpj": FEIRA_CNPJ, "nome": FEIRA_POSTO})
    else:
        empresa_id = empresa["id"]

    # A câmera precisa nascer com o `empresa_id` do posto: `_validar_camera_do_posto`
    # recusa o bico depois se ela pertencer a outro (ou a nenhum).
    cams = banco.cameras_listar(empresa_id=empresa_id)
    dados_cam = {
        "nome": "Câmera da demonstração",
        "empresa_id": empresa_id,
        "local": "estande",
        "camera_tipo": tipo,
        "camera_indice": str(payload.get("camera_indice") or "0"),
        "intelbras_host": (payload.get("intelbras_host") or "").strip(),
        "intelbras_porta": str(payload.get("intelbras_porta") or "554"),
        "intelbras_usuario": (payload.get("intelbras_usuario") or "admin").strip(),
        "intelbras_canal": str(payload.get("intelbras_canal") or "1"),
        "intelbras_subtype": str(payload.get("intelbras_subtype") or "1"),
        "intelbras_formato": (payload.get("intelbras_formato") or "padrao").strip(),
        "rtsp_url_custom": (payload.get("rtsp_url_custom") or "").strip(),
        "ativo": True,
    }
    if payload.get("intelbras_senha"):
        dados_cam["intelbras_senha"] = payload["intelbras_senha"]
    if cams:
        camera_id = cams[0]["id"]
        banco.cameras_atualizar(camera_id, dados_cam)
    else:
        camera_id = banco.cameras_inserir(dados_cam)

    autos = banco.automacoes_listar(empresa_id=empresa_id)
    automacao_id = autos[0]["id"] if autos else banco.automacoes_inserir(
        {"empresa_id": empresa_id, "codigo": FEIRA_CODIGO, "nome": "Demonstração"})

    bicos = banco.bicos_listar(automacao_id=automacao_id)
    if bicos:
        bico_id = bicos[0]["id"]
        banco.bicos_atualizar(bico_id, {"automacao_id": automacao_id, "codigo": FEIRA_CODIGO,
                                        "nome": "Bico da demonstração", "camera_id": camera_id,
                                        "ativo": True})
    else:
        bico_id = banco.bicos_inserir({"automacao_id": automacao_id, "codigo": FEIRA_CODIGO,
                                       "nome": "Bico da demonstração", "camera_id": camera_id})

    # ROI = quadro inteiro. Sem ROI o bico até funciona (a leitura analisa o quadro todo),
    # mas o posto aparece como "não pronto" em /postos — parece quebrado bem na hora da
    # demonstração. A captura ainda serve de teste: confirma que a câmera responde ANTES
    # de declararmos sucesso, e dá as dimensões REAIS (a webcam costuma ignorar o
    # 1280x720 pedido e entregar outra resolução).
    aviso_camera = ""
    frame = camera_mod.capturar_frame_unico(
        tipo=tipo,
        indice=dados_cam["rtsp_url_custom"] or dados_cam["camera_indice"],
        largura=int(cfg.get("camera_largura", "1280")),
        altura=int(cfg.get("camera_altura", "720")),
        fps=int(cfg.get("camera_fps", "15")),
        intelbras={"host": dados_cam["intelbras_host"], "porta": dados_cam["intelbras_porta"],
                   "usuario": dados_cam["intelbras_usuario"],
                   "senha": payload.get("intelbras_senha") or cfg.get("intelbras_senha", ""),
                   "canal": dados_cam["intelbras_canal"],
                   "subtype": dados_cam["intelbras_subtype"],
                   "formato": dados_cam["intelbras_formato"],
                   "rtsp_transporte": cfg.get("rtsp_transporte", "tcp")},
        silencioso=True,
    )
    if frame is not None:
        altura, largura = frame.shape[:2]
        banco.bico_salvar_roi(bico_id, {"x": 0, "y": 0, "w": int(largura), "h": int(altura)})
    else:
        # NÃO é erro: o cadastro está montado e o operador pode ajustar a câmera e
        # redesenhar a área depois. Falhar aqui obrigaria a refazer tudo por causa de um
        # cabo solto.
        aviso_camera = ("a câmera não respondeu. O cadastro foi criado, mas confira o "
                        "índice/endereço em Câmeras e desenhe a área do bico")

    # O supervisor NÃO descobre câmera nova: ele só itera os pipelines já em execução.
    # Quem sobe é a rota, em thread de fundo — mesmo caminho de `POST /api/cameras`.
    cam = banco.cameras_obter(camera_id)
    if cam and cam["ativo"]:
        import threading
        threading.Thread(
            target=api_rotas._iniciar_camera_bg,
            args=(camera_id, pipeline_mod._cfg_para_camera(cfg, cam)),
            daemon=True, name=f"alpr-start-{camera_id}",
        ).start()

    # ARMA o mock neste posto. Enquanto `feira_empresa_id` está vazio, `feira_ativo=sim`
    # não mocka nada (falha fechada) — ver app/visao/feira.py.
    cfg["feira_empresa_id"] = str(empresa_id)
    config.salvar(cfg)

    quem_id, quem_nome = deps.quem_pede(request)
    banco.auditoria_registrar(usuario_id=quem_id, usuario_nome=quem_nome,
                              acao="feira_posto_criado", alvo_tipo="empresa", alvo_id=empresa_id,
                              detalhe=f"camera={tipo} bico={bico_id}")

    return {
        "empresa_id": empresa_id, "entidade_id": entidade_id, "camera_id": camera_id,
        "automacao_id": automacao_id, "bico_id": bico_id,
        "cnpj": FEIRA_CNPJ, "entidade": FEIRA_ENTIDADE, "nome": FEIRA_POSTO,
        "url_leitura": (f"/api/leitura?entidade={FEIRA_ENTIDADE}&cnpj={FEIRA_CNPJ}"
                        f"&automacao={FEIRA_CODIGO}&bico={FEIRA_CODIGO}"),
        "aviso": aviso_camera,
    }


@router.put("/feira/posto", dependencies=[Depends(deps.exigir_admin)])
def feira_apontar_posto(payload: dict, request: Request):
    """Aponta o modo feira para um posto que JÁ EXISTE, em vez de criar um novo.

    `POST /feira/posto` monta a árvore do zero, e é o caminho de quem chega com a máquina
    limpa. Mas quem já cadastrou posto, câmera, automação e bico — e já desenhou as áreas
    — não deveria ser obrigado a montar um segundo posto só para demonstrar: o mock ficava
    inalcançável no cadastro real, e o interruptor em "Sim" não fazia nada.

    Foi exatamente o que aconteceu em campo (03/09/2026): `feira_ativo=sim`,
    `feira_placas=MOK3H92,DDR1989`, e a leitura devolveu DDR1887 sem mockar. O casamento
    estava certo (distância 2, dentro da tolerância) — faltava o ARMAMENTO, porque
    `feira_empresa_id` estava vazio e o escopo é fail-closed.

    `empresa_id: null` DESARMA sem apagar nada — o contrário de `DELETE`, que apaga o
    posto. São ações diferentes de propósito: apontar para outro posto, ou parar de
    mockar, não podem exigir destruir cadastro.
    """
    cfg = config.carregar()
    bruto = payload.get("empresa_id")

    if bruto in (None, "", 0):
        cfg["feira_empresa_id"] = ""
        config.salvar(cfg)
        quem_id, quem_nome = deps.quem_pede(request)
        banco.auditoria_registrar(usuario_id=quem_id, usuario_nome=quem_nome,
                                  acao="feira_desarmada", alvo_tipo="config")
        return {"armado": False, "empresa_id": None}

    empresa_id = deps.inteiro_ou_400(bruto, "empresa_id")
    emp = banco.empresas_obter(empresa_id)
    if not emp:
        raise HTTPException(404, "Posto não encontrado")

    cfg["feira_empresa_id"] = str(empresa_id)
    config.salvar(cfg)
    quem_id, quem_nome = deps.quem_pede(request)
    banco.auditoria_registrar(usuario_id=quem_id, usuario_nome=quem_nome,
                              acao="feira_posto_apontado", alvo_tipo="empresa",
                              alvo_id=empresa_id, detalhe=f"nome={emp['nome']}")
    log.warning("MODO FEIRA apontado para o posto %s (%s): leituras dele passam a ser "
                "mockadas quando a placa casar.", empresa_id, emp["nome"])
    return {"armado": True, "empresa_id": empresa_id, "nome": emp["nome"],
            "cnpj": emp["cnpj"]}


@router.delete("/feira/posto", dependencies=[Depends(deps.exigir_admin)])
def feira_remover_posto(request: Request):
    """Apaga o posto de demonstração e DESARMA o mock.

    `empresas_remover` já derruba bicos → automações → câmeras → empresa na ordem certa
    (o RESTRICT de `bicos.camera_id` faria um DELETE ingênuo falhar).
    """
    cfg = config.carregar()
    bruto = (cfg.get("feira_empresa_id") or "").strip()
    empresa_id = int(bruto) if bruto.isdigit() else None
    if empresa_id is None:
        raise HTTPException(404, "Nenhum posto de demonstração configurado")

    # Para os pipelines das câmeras do posto ANTES de apagar: liberar a conexão depois de
    # a linha sumir deixaria a thread apontando para cadastro inexistente.
    from app.visao import pipeline as pipeline_mod
    for cam in banco.cameras_listar(empresa_id=empresa_id):
        try:
            pipeline_mod.parar_camera(cam["id"])
        except Exception as e:
            log.warning("Falha ao parar câmera %s do posto de demonstração: %s", cam["id"], e)

    removido = banco.empresas_remover(empresa_id)
    cfg["feira_empresa_id"] = ""
    config.salvar(cfg)

    quem_id, quem_nome = deps.quem_pede(request)
    banco.auditoria_registrar(usuario_id=quem_id, usuario_nome=quem_nome,
                              acao="feira_posto_removido", alvo_tipo="empresa",
                              alvo_id=empresa_id)
    return {"removido": removido, "desarmado": True}


@router.get("/feira/posto", dependencies=[Depends(deps.exigir_admin)])
def feira_estado_posto():
    """Estado do posto de demonstração, para a seção secreta da tela de configuração."""
    cfg = config.carregar()
    bruto = (cfg.get("feira_empresa_id") or "").strip()
    empresa = banco.empresas_obter(int(bruto)) if bruto.isdigit() else None
    if empresa is None:
        return {"existe": False, "armado": False}
    return {
        "existe": True,
        # ARMADO = o mock pode de fato agir. Separado de "existe" porque o posto pode estar
        # criado com o interruptor ainda desligado, e a tela precisa dizer qual dos dois.
        "armado": config.get_bool(cfg, "feira_ativo") and bool(cfg.get("feira_placas")),
        "empresa_id": empresa["id"], "nome": empresa["nome"], "cnpj": empresa["cnpj"],
        "url_leitura": (f"/api/leitura?entidade={FEIRA_ENTIDADE}&cnpj={empresa['cnpj']}"
                        f"&automacao={FEIRA_CODIGO}&bico={FEIRA_CODIGO}"),
    }


@router.get("/feira/fichas", dependencies=[Depends(deps.exigir_admin)])
def feira_fichas_obter():
    """As fichas de demonstração por placa — o que o card "Bem-vindo!" da vitrine exibe.

    Só exibição (combustível/modelo/cor/ano/mensagem), guardado à parte do cache real de
    veículos. Ver `app/visao/feira_fichas.py`.
    """
    return {"fichas": feira_fichas.carregar_fichas()}


@router.put("/feira/fichas", dependencies=[Depends(deps.exigir_admin)])
def feira_fichas_salvar(payload: dict, request: Request):
    """Grava o conjunto completo de fichas (o editor manda tudo — sem merge).

    `payload["fichas"]` = { "MOK3H92": {apelido, modelo, combustivel, cor, ano, mensagem} }.
    """
    fichas = payload.get("fichas")
    if not isinstance(fichas, dict):
        raise HTTPException(422, "Esperado objeto 'fichas' { placa: {campos...} }")
    gravado = feira_fichas.salvar_fichas(fichas)
    quem_id, quem_nome = deps.quem_pede(request)
    banco.auditoria_registrar(usuario_id=quem_id, usuario_nome=quem_nome,
                              acao="feira_fichas_salvas", alvo_tipo="config",
                              detalhe=f"{len(gravado)} ficha(s)")
    return {"fichas": gravado}


# Cadência do kiosk. Bem mais folgado que o botão de teste (`_LIMITE_LER_PLACA_MIN=6`)
# porque a vitrine escaneia sozinha em loop: ~1,5 s entre leituras cabe aqui. O teto
# continua sendo só freio contra abuso — o modo feira já está fora da taxa de produção,
# e o bico é resolvido no servidor (o cliente não escolhe posto), então não dá para
# varrer cadastro alheio por aqui.
_LIMITE_FEIRA_SCAN_MIN = 45


@router.post("/feira/scan")
def feira_scan(request: Request, forcar: bool = False):
    """Uma leitura do bico de demonstração para o kiosk `/feira` (loop hands-free).

    Resolve o bico da demonstração a partir de `feira_empresa_id` — sem id vindo do
    cliente, então não serve para ler bico de outro posto. `origem="feira"`: é
    demonstração, fica fora da métrica de produção. Devolve só o que a vitrine precisa,
    incluindo a ficha local da placa reconhecida.

    `forcar=1` é o botão "Forçar leitura" do kiosk: usa o perfil COMPLETO (mais fotos, mais
    robusto) em vez do rápido do loop automático, para o caso do carrinho estar num ângulo
    que o rápido não fecha. O loop hands-free continua usando o rápido (forcar=0).
    """
    cfg = config.carregar()
    if not feira.ativo(cfg):
        raise HTTPException(409, "Modo feira não está armado")

    ip = request.client.host if request.client else "?"
    if not limitador.permitido("feira_scan", ip, _LIMITE_FEIRA_SCAN_MIN, 60):
        raise HTTPException(429, "Muitas leituras seguidas. Aguarde um instante.")

    perfil = leitura.PERFIL_COMPLETO if forcar else leitura.PERFIL_RAPIDO

    empresa_id = feira.empresa_demo(cfg)
    bico = None
    for auto in banco.automacoes_listar(empresa_id=empresa_id):
        candidatos = banco.bicos_listar(automacao_id=auto["id"])
        if candidatos:
            bico = candidatos[0]
            break
    if bico is None:
        raise HTTPException(404, "Posto de demonstração sem bico configurado")

    bico_full, motivo = banco.bico_verificar_ativo(bico["id"])
    if bico_full is None:
        raise HTTPException(409, "O bico da demonstração está desativado no cadastro")
    fontes_db, avisos, falha = banco.cameras_do_bico(bico_full)
    if falha is not None:
        raise HTTPException(404, "Câmera da demonstração não encontrada")

    try:
        resultado = leitura.ler_placa(
            fontes=leitura_rotas.montar_fontes(fontes_db, cfg, perfil),
            cfg=cfg, avisos=avisos, preview_nome=f"preview_bico_{bico['id']}",
            bico_id=bico["id"], origem="feira", perfil=perfil,
            empresa_id=empresa_id,
        )
    except leitura.LeituraError as e:
        raise HTTPException(e.status, e.mensagem)

    placa = resultado.get("placa")
    return {
        "placa": placa,
        "confianca": resultado.get("confianca"),
        # O mock casou a placa? É o que o kiosk usa para decidir se revela o card de
        # veículo de demonstração. Antes ele olhava `origem === 'feira'`, que aqui vale
        # SEMPRE (este endpoint pede `origem="feira"` para a leitura ficar fora de
        # 'producao') — a placa do celular de um visitante seria saudada como carrinho.
        "mockada": bool(resultado.get("mockada")),
        "confirmada": resultado.get("confirmada"),
        "tipo_veiculo": resultado.get("tipo_veiculo"),
        "ficha": feira_fichas.ficha_de(placa) if placa else None,
        # O MESMO bloco que o GET do roteador devolve. O kiosk mostra a `ficha` (rótulos
        # humanos, `apelido`/`mensagem`); isto aqui existe para a demo poder exibir, na
        # própria tela, o JSON que o posto vai receber — que é o que se está vendendo.
        "veiculo": feira_fichas.bloco_de_leitura(resultado),
    }
