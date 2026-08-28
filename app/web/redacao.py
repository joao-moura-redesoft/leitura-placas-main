"""Remove segredos das linhas de banco antes de elas virarem JSON.

Auditoria de 27/08/2026, achado K3 — PROVADO executando com sessão real de cada papel:

    ===== como CLIENTE =====            ===== como OPERADOR =====
      /api/cameras   200  SENHA DO DVR    /api/cameras   200  SENHA DO DVR
      /api/empresas  200  API_KEY         /api/empresas  200  API_KEY
      /api/postos    200  API_KEY         /api/postos    200  API_KEY

`cameras_listar` e `empresas_listar` fazem `SELECT *`, e as rotas que as devolvem não tinham
gate de admin. Vazavam duas credenciais com impactos distintos:

  intelbras_senha    acesso direto ao DVR e à rede do posto. É credencial de infraestrutura
                     da RedSoft, não do cliente.
  empresas.api_key   deixa qualquer papel chamar /api/leitura e o preview de QUALQUER posto.

**Por que aqui e não no banco.** O `SELECT *` tem de continuar trazendo a senha: quem abre a
câmera (`pipeline`, `supervisor`, `captura_dataset`, `leitura`) precisa dela. O segredo não é
o problema; o problema é ele atravessar a fronteira HTTP. Então a redação é da CAMADA WEB, e
o dado interno segue intacto.

**Por que não bastava pôr `exigir_admin` nas rotas.** Um `cliente` legitimamente vê as câmeras
do próprio posto — é a tela dele. Fechar a rota tiraria a tela; redigir o campo mantém a tela
e tira o segredo. O padrão já existia no projeto (`CHAVES_SENSIVEIS` em `/api/config`), só não
tinha sido aplicado aqui.

Admin que precisa EDITAR a credencial usa `GET /api/cameras/{id}/credenciais`, que tem gate
próprio — ver `app/web/api.py`.
"""
from __future__ import annotations

# Colunas que nunca podem sair numa resposta HTTP de listagem/detalhe.
#
# `senha` avulso cobre `_origem_rtsp`, que aceita a chave curta; `rtsp_url_custom` entra
# porque a URL RTSP carrega usuário e senha embutidos (`rtsp://user:pass@host/...`) — redigir
# só `intelbras_senha` e deixar a URL passar não esconderia nada.
SEGREDOS_CAMERA = ("intelbras_senha", "senha", "rtsp_url_custom")
SEGREDOS_EMPRESA = ("api_key",)

# O que a tela mostra no lugar. String vazia seria ambígua com "não configurado", e a
# diferença importa: o admin precisa saber se falta cadastrar a senha ou se ela existe e só
# está escondida. Mesma convenção de `api.py:CHAVES_SENSIVEIS`.
MASCARA = "********"


def redigir(linha: dict | None, campos: tuple[str, ...]) -> dict | None:
    """Mascara `campos` em `linha` — pública para qualquer chamador com a mesma
    necessidade (ex.: `/api/config` e seu `CHAVES_SENSIVEIS`, achado A7)."""
    if linha is None:
        return None
    # Cópia: as linhas vêm de `dict(row)` e podem estar em cache do chamador (ex.: o dict
    # `cams` de `/api/postos/{id}`). Redigir in-place apagaria a senha para quem ainda vai
    # usá-la na mesma request.
    saida = dict(linha)
    for campo in campos:
        if campo in saida:
            saida[campo] = MASCARA if saida[campo] else ""
    return saida


def camera(linha: dict | None) -> dict | None:
    """Uma câmera pronta para virar JSON."""
    return redigir(linha, SEGREDOS_CAMERA)


def cameras(linhas) -> list[dict]:
    return [redigir(c, SEGREDOS_CAMERA) for c in linhas]


def descartar_mascara(payload: dict, campos: tuple[str, ...] = SEGREDOS_CAMERA) -> dict:
    """Tira do payload os campos que voltaram MASCARADOS, para eles não sobrescreverem o
    valor real.

    Sem isto, redigir na leitura vira perda de dado na escrita. A tela de câmeras carrega
    `rtsp_url_custom` no formulário (`cameras.html`) e o reenvia INTEIRO no save — então
    editar só o nome de uma câmera gravava a máscara por cima da URL de conexão e a câmera
    parava de funcionar. (Regressão introduzida junto com a redação do achado K3 e pega na
    revisão de 27/08/2026.)

    `intelbras_senha` já estava a salvo por acaso: o formulário só envia a senha quando o
    operador digita uma nova. Este filtro torna isso garantia em vez de sorte, e vale para
    qualquer tela futura que faça round-trip.
    """
    return {k: v for k, v in payload.items() if not (k in campos and v == MASCARA)}


def empresa(linha: dict | None) -> dict | None:
    """Um posto pronto para virar JSON."""
    return redigir(linha, SEGREDOS_EMPRESA)


def empresas(linhas) -> list[dict]:
    return [redigir(e, SEGREDOS_EMPRESA) for e in linhas]
