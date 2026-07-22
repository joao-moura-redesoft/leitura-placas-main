"""CRUD administrativo do cadastro multi-tenant: entidades/empresas/automacoes/bicos.

Painel único da equipe RedSoft (sem login por cliente) — cadastro 100% manual, sem
replicação. Mesmo padrão de validação/erros já usado no CRUD de câmeras (app/web/api.py).
"""
from __future__ import annotations
import json
import re
import sqlite3

from fastapi import APIRouter, HTTPException

from app.core import banco
from app.core import config
from app.visao import leitura
from app.web import leitura as leitura_rotas

router = APIRouter(prefix="/api")


def _normalizar_cnpj(cnpj: str) -> str:
    return re.sub(r"\D", "", cnpj or "")


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
def postos_listar():
    entidades = {e["id"]: e for e in banco.entidades_listar()}
    automacoes = banco.automacoes_listar()
    bicos = banco.bicos_listar()
    saida = []
    for emp in banco.empresas_listar():
        autos = [a for a in automacoes if a["empresa_id"] == emp["id"]]
        ids = {a["id"] for a in autos}
        meus_bicos = [b for b in bicos if b["automacao_id"] in ids]
        cams = banco.cameras_listar(empresa_id=emp["id"])
        ent = entidades.get(emp["entidade_id"])
        saida.append({
            **emp,
            "entidade_nome": ent["nome"] if ent else "",
            "n_automacoes": len(autos),
            "n_bicos": len(meus_bicos),
            "n_cameras": len(cams),
            "n_bicos_sem_roi": sum(1 for b in meus_bicos if not b["roi"]),
            # "pronto" = dá para o roteador chamar e obter leitura útil
            "pronto": bool(cams and autos and meus_bicos and all(b["roi"] for b in meus_bicos)),
        })
    saida.sort(key=lambda p: (p["entidade_nome"], p["nome"]))
    return saida


@router.get("/postos/{empresa_id}")
def posto_detalhe(empresa_id: int):
    emp = banco.empresas_obter(empresa_id)
    if not emp:
        raise HTTPException(404, "Posto não encontrado")
    ent = banco.entidades_obter(emp["entidade_id"])
    cams = {c["id"]: c for c in banco.cameras_listar(empresa_id=empresa_id)}
    # `ao_vivo` = há pipeline contínuo com o stream aberto → a tela pode exibir MJPEG
    # em vez de captura sob demanda.
    from app.visao import pipeline as pipeline_mod
    for c in cams.values():
        c["ao_vivo"] = c["id"] in pipeline_mod._instancias
    autos = []
    for a in banco.automacoes_listar(empresa_id=empresa_id):
        bicos = []
        for b in banco.bicos_listar(automacao_id=a["id"]):
            cam = cams.get(b["camera_id"]) or banco.cameras_obter(b["camera_id"]) or {}
            bicos.append({**b,
                          "camera_nome": cam.get("nome", "?"),
                          "camera_local": cam.get("local", ""),
                          "tem_roi": bool(b["roi"])})
        autos.append({**a, "bicos": bicos})
    return {
        "empresa": emp,
        "entidade": ent,
        "cameras": list(cams.values()),
        "automacoes": autos,
    }


# ─── Entidades ───────────────────────────────────────────────────────────────

@router.get("/entidades")
def entidades_listar():
    return banco.entidades_listar()


@router.post("/entidades")
def entidades_inserir(payload: dict):
    nome = (payload.get("nome") or "").strip()
    if not nome:
        raise HTTPException(400, "nome é obrigatório")
    return {"id": banco.entidades_inserir({**payload, "nome": nome})}


@router.put("/entidades/{id_}")
def entidades_atualizar(id_: int, payload: dict):
    nome = (payload.get("nome") or "").strip()
    if not nome:
        raise HTTPException(400, "nome é obrigatório")
    if not banco.entidades_atualizar(id_, {**payload, "nome": nome}):
        raise HTTPException(404, "Entidade não encontrada")
    return {"atualizado": True}


@router.delete("/entidades/{id_}")
def entidades_remover(id_: int):
    if not banco.entidades_remover(id_):
        raise HTTPException(404, "Entidade não encontrada")
    return {"removido": True}


# ─── Empresas (CNPJ = 1 posto físico) ───────────────────────────────────────

@router.get("/empresas")
def empresas_listar(entidade_id: int | None = None):
    return banco.empresas_listar(entidade_id=entidade_id)


@router.post("/empresas")
def empresas_inserir(payload: dict):
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
    return {"id": id_}


@router.put("/empresas/{id_}")
def empresas_atualizar(id_: int, payload: dict):
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
    return {"atualizado": True}


@router.delete("/empresas/{id_}")
def empresas_remover(id_: int):
    if not banco.empresas_remover(id_):
        raise HTTPException(404, "Empresa não encontrada")
    return {"removido": True}


# ─── Automações (até 2 por posto) ───────────────────────────────────────────

@router.get("/automacoes")
def automacoes_listar(empresa_id: int | None = None):
    return banco.automacoes_listar(empresa_id=empresa_id)


@router.post("/automacoes")
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


@router.put("/automacoes/{id_}")
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


@router.delete("/automacoes/{id_}")
def automacoes_remover(id_: int):
    if not banco.automacoes_remover(id_):
        raise HTTPException(404, "Automação não encontrada")
    return {"removido": True}


# ─── Bicos ───────────────────────────────────────────────────────────────────

@router.get("/bicos")
def bicos_listar(automacao_id: int | None = None, camera_id: int | None = None):
    return banco.bicos_listar(automacao_id=automacao_id, camera_id=camera_id)


@router.get("/bicos/{id_}")
def bicos_obter(id_: int):
    bico = banco.bicos_obter(id_)
    if not bico:
        raise HTTPException(404, "Bico não encontrado")
    return bico


def _validar_bico(payload: dict) -> None:
    """Garante que a câmera escolhida pertence ao MESMO posto do bico.

    Sem isso, num servidor central seria possível apontar o bico do Posto A para a câmera
    do Posto B — o roteador de um cliente receberia a imagem do pátio de outro.
    """
    if not payload.get("automacao_id") or not payload.get("camera_id"):
        raise HTTPException(400, "automacao_id e camera_id são obrigatórios")

    automacao = banco.automacoes_obter(int(payload["automacao_id"]))
    if not automacao:
        raise HTTPException(400, "Automação não encontrada")
    camera = banco.cameras_obter(int(payload["camera_id"]))
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


@router.post("/bicos")
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


@router.put("/bicos/{id_}")
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


@router.delete("/bicos/{id_}")
def bicos_remover(id_: int):
    if not banco.bicos_remover(id_):
        raise HTTPException(404, "Bico não encontrado")
    return {"removido": True}


@router.put("/bicos/{id_}/roi")
def bicos_salvar_roi(id_: int, payload: dict):
    """Salva a área de captura (ROI) do bico. Bicos que compartilham câmera mantêm ROIs
    independentes — a leitura reativa recorta pela área do bico chamado no GET.
    """
    try:
        x, y, w, h = int(payload["x"]), int(payload["y"]), int(payload["w"]), int(payload["h"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "x, y, w, h são obrigatórios e devem ser inteiros")
    if w <= 0 or h <= 0:
        raise HTTPException(400, "w e h devem ser positivos")
    if not banco.bicos_obter(id_):
        raise HTTPException(404, "Bico não encontrado")
    banco.bico_salvar_roi(id_, {"x": x, "y": y, "w": w, "h": h})
    return {"salvo": True}


@router.delete("/bicos/{id_}/roi")
def bicos_limpar_roi(id_: int):
    """Remove a área de captura — leitura reativa desse bico volta a usar o frame completo."""
    if not banco.bicos_obter(id_):
        raise HTTPException(404, "Bico não encontrado")
    banco.bico_limpar_roi(id_)
    return {"limpo": True}


@router.post("/bicos/{id_}/ler-placa-teste")
def bicos_ler_placa_teste(id_: int):
    """Testa a leitura de um bico direto (sem montar a URL completa de
    entidade+cnpj+automacao+bico) — usado pelo editor de ROI pra validar a área recém-desenhada.

    Passa pelo mesmo gate de `ativo` (entidade/empresa/automação/bico/câmera) que a
    leitura reativa de verdade — senão o botão de teste do painel responde mesmo para
    um cadastro desativado, driblando a trava aplicada em produção.
    """
    bico, motivo = banco.bico_verificar_ativo(id_)
    if bico is None:
        if motivo in ("bico", "empresa", "automacao"):
            raise HTTPException(404, "Bico não encontrado")
        nivel = motivo.rsplit("_", 1)[0]
        raise HTTPException(409, f"Nível '{nivel}' está desativado no cadastro")
    cam = banco.cameras_obter(bico["camera_id"])
    if not cam:
        raise HTTPException(404, "Câmera do bico não encontrada")

    cfg = config.carregar()
    especificacao = leitura.EspecificacaoCamera.de_camera_db(cam, cfg)
    roi = json.loads(bico["roi"]) if bico.get("roi") else None
    try:
        return leitura.ler_placa(
            camera_id=bico["camera_id"], especificacao=especificacao, roi=roi, cfg=cfg,
            pipeline_frame_provider=leitura_rotas.frame_ao_vivo(bico["camera_id"]),
            preview_nome=f"preview_bico_{id_}", bico_id=id_, origem="teste",
        )
    except leitura.LeituraError as e:
        raise HTTPException(e.status, e.mensagem)
