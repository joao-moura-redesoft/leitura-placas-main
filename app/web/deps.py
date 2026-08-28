"""Dependências FastAPI de autorização por usuário (RBAC leve: admin × operador × cliente).

`_AuthMiddleware` (app/servidor.py) já garante que só chega às rotas quem está
autenticado (sessão ou api_key global do servidor) — o que falta aqui é DIFERENCIAR
quem chegou. Três papéis:

- `admin`: vê e edita tudo, inclusive configuração do sistema e cadastro estrutural.
- `operador`: NÃO preso a um posto (vê todos, como admin) mas não administra —
  mesmo gate de `exigir_admin` que barra 'cliente' também barra 'operador'.
- `cliente`: preso a UM posto (`empresa_id`), só vê os dados dele.

A distinção nunca é feita direto de `banco.buscar_usuario_id` nas rotas — sempre por
aqui, para o escopo ficar num lugar só.
"""
from __future__ import annotations
import threading

from fastapi import HTTPException, Request


def inteiro_ou_400(valor, campo: str) -> int:
    """`int()` com erro de CLIENTE, não 500.

    `int(payload["empresa_id"])` cru levantava ValueError num payload como
    `{"empresa_id": "abc"}`, e o FastAPI devolvia 500 — erro de servidor para um dado
    inválido do cliente, ainda deixando stack no log como se a falha fosse nossa.
    (Auditoria 27/08/2026.) Centralizado aqui porque estava copiado igual em
    app/web/api.py, app/web/cadastro.py e app/web/usuarios.py.
    """
    try:
        return int(valor)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{campo} deve ser um número inteiro")


def usuario_atual(request: Request) -> dict | None:
    """Usuário autenticado por SESSÃO (dict com papel/empresa_id — ver
    `_AuthMiddleware`). None quando a requisição só passou pela api_key GLOBAL do
    servidor (integração/automação sem usuário — tratada como admin, igual ao
    comportamento de antes deste módulo existir) ou numa rota pública."""
    return getattr(request.state, "user", None)


def eh_admin(request: Request) -> bool:
    user = usuario_atual(request)
    return user is None or user.get("papel") == "admin"


def exigir_admin(request: Request) -> None:
    """Dependency para rotas restritas a administrador: cadastro estrutural
    (entidade/posto/automação/bico/câmera), configuração do sistema, ferramentas
    internas de diagnóstico/teste, gestão de usuários. Usuário 'cliente' recebe 403."""
    if not eh_admin(request):
        raise HTTPException(403, "Ação restrita a administradores.")


def empresa_do_usuario(request: Request) -> int | None:
    """empresa_id ao qual o usuário logado está restrito — None significa SEM
    restrição (admin, operador, ou acesso via api_key global): todo código que
    consome esta função trata `None` como "não filtra". 'operador' não é preso a
    posto nenhum — a diferença dele pro admin é só não passar em `exigir_admin`.

    Um usuário 'cliente' cuja empresa foi apagada (`empresa_id` cai para NULL via
    `ON DELETE SET NULL`, ver banco.py) NÃO pode devolver None aqui — isso seria lido
    como "sem restrição" e destravaria acesso a TUDO. Devolve -1 (id que nunca
    existe): toda comparação de escopo falha e o usuário órfão não vê nada, em vez de
    ver tudo. `banco.empresas_remover`/`_apagar_empresas` também desativam esses
    usuários — isto aqui é a segunda camada, não a única.
    """
    user = usuario_atual(request)
    if user is None or user.get("papel") in ("admin", "operador"):
        return None
    return user.get("empresa_id") if user.get("empresa_id") is not None else -1


def quem_pede(request: Request) -> tuple[int | None, str]:
    """(usuario_id, usuario_nome) de quem está fazendo a ação — pra auditoria
    (app/core/banco.py:auditoria_registrar). (None, "api_key") quando a requisição
    veio só pela api_key global (sem usuário associado)."""
    user = usuario_atual(request)
    return (user["id"], user["nome"]) if user is not None else (None, "api_key")


def checar_acesso_empresa(request: Request, empresa_id: int | None) -> None:
    """Levanta 404 se o usuário logado é 'cliente' de OUTRA empresa (ou de nenhuma —
    `empresa_id=None` no recurso, ex.: câmera órfã, não pertence a ninguém).

    404 e não 403: para um usuário 'cliente' não confirmamos nem que o recurso existe
    fora do escopo dele — mesmo padrão que `banco.resolver_bico` já usa para não
    vazar a existência de cadastro de outro cliente.
    """
    escopo = empresa_do_usuario(request)
    if escopo is not None and empresa_id != escopo:
        raise HTTPException(404, "Não encontrado")


def chave_do_posto_do_bico(bico_id: int) -> str:
    """api_key PRÓPRIA do posto dono deste bico ('' = posto sem chave, público).

    Usada pelo `_AuthMiddleware` para liberar o preview de um bico a quem apresenta a
    chave daquele posto — é a mesma credencial que o roteador já manda em `/api/leitura`
    (ver app/web/leitura.py:leitura_reativa). Fica aqui, e não no middleware, para a
    resolução bico→automação→posto não virar consulta solta no meio do pipeline HTTP.
    """
    from app.core import banco

    bico = banco.bicos_obter(bico_id)
    if not bico:
        return ""
    automacao = banco.automacoes_obter(bico["automacao_id"])
    if not automacao:
        return ""
    empresa = banco.empresas_obter(automacao["empresa_id"])
    return (empresa or {}).get("api_key") or ""


# ── Cache de empresa_id por câmera, para o HLS ──────────────────────────────────────
#
# `_HlsPorPosto.get_response` (app/servidor.py) chamava `banco.cameras_obter` a CADA
# request — inclusive cada segmento .ts, buscado a cada poucos segundos por câmera por
# espectador. Mesmo padrão de cache já usado em app/streaming/stream.py (`_cache_jpeg`):
# dict plano + lock, invalidado EXPLICITAMENTE (nunca por TTL) quando a câmera muda de
# dono ou é removida. (Achado A4/C1, review de 28/08/2026.)
_cache_lock_camera = threading.Lock()
_AUSENTE = object()   # sentinela: câmera não existe (evita reconsultar toda hora)
_cache_empresa_camera: dict[int, object] = {}


def empresa_da_camera_cacheada(camera_id: int):
    """empresa_id da câmera, `None` se ela não tem posto, ou `_AUSENTE` se não existe."""
    with _cache_lock_camera:
        if camera_id in _cache_empresa_camera:
            return _cache_empresa_camera[camera_id]
    from app.core import banco

    cam = banco.cameras_obter(camera_id)
    valor = cam.get("empresa_id") if cam else _AUSENTE
    with _cache_lock_camera:
        _cache_empresa_camera[camera_id] = valor
    return valor


def descartar_cache_camera(camera_id: int) -> None:
    with _cache_lock_camera:
        _cache_empresa_camera.pop(camera_id, None)


def limpar_cache_camera() -> None:
    with _cache_lock_camera:
        _cache_empresa_camera.clear()
