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
from fastapi import HTTPException, Request


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
