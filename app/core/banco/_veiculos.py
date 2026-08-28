"""Cache dos dados de veículo consultados na apiplacas.com.br.

A regra de VALIDADE (TTL) mora aqui, num lugar só: quem chama pergunta "tem dado
utilizável para esta placa?" em vez de recalcular prazo por conta própria. Dois lugares
calculando vencimento é como se paga duas vezes pela mesma placa.

Esta camada não fala com a rede — quem consulta a API é `app/integracoes/apiplacas.py`,
pelo mesmo motivo que `_deteccoes` não faz I/O de arquivo.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ._base import _agora, cursor

# Veredito sobre o VEÍCULO, não sobre a chamada HTTP — ver o comentário da coluna em
# `_esquema.py`. Validado aqui em Python e não por CHECK: um CHECK não dá para estender
# sem recriar a tabela (mesmo motivo de `tipo_veiculo_fonte`).
STATUS_VEICULO = ("ok", "inexistente")

# Colunas curadas, na ordem do INSERT. Uma tupla só, usada pelo INSERT e pelo UPDATE do
# upsert: manter duas listas paralelas é exatamente como uma coluna nova entra no banco
# e nunca chega ao payload.
CAMPOS_CURADOS = (
    "combustivel", "combustivel_sigla", "marca", "modelo", "ano", "ano_modelo",
    "cor", "especie", "tipo_veiculo", "situacao", "municipio", "uf",
)


def _validar_status(status: str) -> None:
    if status not in STATUS_VEICULO:
        raise ValueError(
            f"status de veículo inválido: {status!r} (esperado um de {STATUS_VEICULO})"
        )


def _idade_dias(iso: str) -> float | None:
    """Quantos dias se passaram desde `iso`. None se a string não for interpretável.

    None (e não 0) porque um `consultado_em` corrompido não deve parecer recém-consultado:
    quem chama trata None como "não sei a idade" e reconsulta, que é o lado seguro — erra
    gastando R$ 0,03, não entregando dado de validade desconhecida como se fosse fresco.
    """
    try:
        quando = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - quando).total_seconds() / 86400.0


def veiculos_obter(placa: str) -> dict | None:
    """Linha desta placa, sem julgar validade. None se a placa nunca foi consultada.

    `bruto` sai como string (o chamador decide se desserializa). Quem quer o dado
    utilizável usa `veiculos_valido`, que aplica o TTL.
    """
    with cursor() as c:
        r = c.execute("SELECT * FROM veiculos WHERE placa = ?", (placa,)).fetchone()
        return dict(r) if r else None


def veiculos_valido(placa: str, ttl_dias: int, ttl_negativo_dias: int) -> dict | None:
    """A linha desta placa SE ainda for utilizável; None se não existe ou venceu.

    Dois prazos porque os dois desfechos vencem por motivos diferentes:

    - `'ok'` vence porque `situacao` (restrição/roubo), `municipio` e FIPE mudam. O que
      motivou a integração — combustível, marca, modelo — não muda nunca.
    - `'inexistente'` vence porque a placa pode ser de veículo recém-emplacado que ainda
      não entrou na base do fornecedor. Mas o caso COMUM é OCR que leu errado, uma placa
      que nunca vai existir — e é por isso que o prazo negativo é separado, em vez de
      reconsultar toda negativa junto com as positivas.

    `ttl_dias <= 0` = nunca vence (o operador desligou a reconsulta). Vale para o prazo
    correspondente ao status da linha, não para os dois.
    """
    linha = veiculos_obter(placa)
    if linha is None:
        return None
    prazo = ttl_negativo_dias if linha["status"] == "inexistente" else ttl_dias
    if prazo <= 0:
        return linha
    idade = _idade_dias(linha["consultado_em"])
    if idade is None or idade >= prazo:
        return None
    return linha


def veiculos_obter_varios(placas: list[str]) -> dict[str, dict]:
    """Cache de várias placas numa consulta só, indexado por placa.

    Existe por causa do histórico: ele lista até 500 leituras por página, e uma consulta
    por linha seriam 500 idas ao banco só para pintar uma coluna. Placas fora do cache
    simplesmente não aparecem no dict — quem chama trata ausência como "sem dados".
    """
    unicas = [p for p in dict.fromkeys(placas) if p]
    if not unicas:
        return {}
    # Em lotes, para não estourar o limite de variáveis de host do SQLite (999 por
    # padrão) quando o chamador passar uma página inteira do histórico.
    saida: dict[str, dict] = {}
    with cursor() as c:
        for i in range(0, len(unicas), 500):
            fatia = unicas[i:i + 500]
            ph = ", ".join("?" * len(fatia))
            for r in c.execute(f"SELECT * FROM veiculos WHERE placa IN ({ph})", fatia):
                saida[r["placa"]] = dict(r)
    return saida


def veiculos_pendentes(limit: int = 20, empresa_id: int | None = None) -> list[dict]:
    """Placas MAIS VISTAS que ainda não têm dados de veículo. `[{placa, vezes}]`.

    É a lista que a consulta em lote propõe. Ordenada por frequência porque, com cota
    curta, o crédito rende mais na frota que volta toda semana do que numa placa vista uma
    vez e nunca mais.

    "Pendente" é NÃO TER LINHA — não inclui cache vencido. Uma placa vencida ainda entrega
    dado bom (só possivelmente desatualizado em `situacao`), e misturá-la aqui faria o lote
    gastar em quem já está atendido, que é o oposto do objetivo.

    Ignora `origem='teste'`: leitura de teste é alguém ajustando enquadramento, não
    movimento do posto, e não deve influenciar onde o dinheiro é gasto.
    """
    sql = ("SELECT d.placa AS placa, COUNT(*) AS vezes "
           "FROM deteccoes d "
           "LEFT JOIN veiculos v ON v.placa = d.placa "
           "WHERE v.placa IS NULL AND d.origem <> 'teste'")
    params: list = []
    if empresa_id is not None:
        # Mesmo escopo por posto que o resto do painel aplica: um cliente não propõe (nem
        # paga) consulta para placa que ele nem pode ver.
        sql += (" AND d.bico_id IN (SELECT b.id FROM bicos b "
                "JOIN automacoes a ON a.id = b.automacao_id WHERE a.empresa_id = ?)")
        params.append(empresa_id)
    sql += " GROUP BY d.placa ORDER BY vezes DESC, d.placa ASC LIMIT ?"
    params.append(limit)
    with cursor() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def veiculos_salvar(placa: str, *, status: str, campos: dict,
                    bruto: str | None = None, http_status: int | None = None,
                    fonte: str = "apiplacas") -> None:
    """Grava/atualiza a linha desta placa e SOMA 1 em `consultas`.

    Upsert (`ON CONFLICT`) e não DELETE+INSERT porque `criado_em` e `consultas` têm de
    sobreviver à reconsulta: sem isso o histórico de gasto zera a cada TTL vencido, e a
    pergunta "quanto essa placa já custou?" fica sem resposta.

    `campos` aceita SÓ as chaves de `CAMPOS_CURADOS`; chave estranha levanta ValueError
    em vez de ser ignorada em silêncio — é assim que um campo novo da API deixaria de ser
    gravado sem ninguém notar.
    """
    _validar_status(status)
    estranhas = set(campos) - set(CAMPOS_CURADOS)
    if estranhas:
        raise ValueError(
            f"campos desconhecidos para veiculos: {sorted(estranhas)} "
            f"(conhecidos: {list(CAMPOS_CURADOS)})"
        )
    agora = _agora()
    valores = [campos.get(k) for k in CAMPOS_CURADOS]
    cols = ", ".join(CAMPOS_CURADOS)
    ph = ", ".join("?" * len(CAMPOS_CURADOS))
    # `consultado_em` é sempre reescrito (é a data DESTA resposta); `criado_em` nunca —
    # daí ele ficar de fora do DO UPDATE.
    sets = ", ".join(f"{k} = excluded.{k}" for k in CAMPOS_CURADOS)
    with cursor() as c:
        c.execute(
            f"INSERT INTO veiculos (placa, status, criado_em, consultado_em, consultas, "
            f"                      http_status, {cols}, bruto, fonte) "
            f"VALUES (?, ?, ?, ?, 1, ?, {ph}, ?, ?) "
            f"ON CONFLICT(placa) DO UPDATE SET "
            f"  status = excluded.status, "
            f"  consultado_em = excluded.consultado_em, "
            f"  consultas = consultas + 1, "
            f"  http_status = excluded.http_status, "
            f"  {sets}, "
            f"  bruto = excluded.bruto, "
            f"  fonte = excluded.fonte",
            (placa, status, agora, agora, http_status, *valores, bruto, fonte),
        )


def veiculos_remover(placa: str) -> bool:
    """Descarta o cache de uma placa. True se havia linha.

    É o "consultar de novo agora" do painel, e a saída de emergência quando a API
    devolveu dado errado para uma placa específica.
    """
    with cursor() as c:
        return c.execute("DELETE FROM veiculos WHERE placa = ?", (placa,)).rowcount > 0


def veiculos_consultas_desde(desde_iso: str) -> int:
    """Quantas consultas PAGAS foram feitas desde `desde_iso` (usa idx_veiculos_consultado).

    É o teto DIÁRIO de gasto. Fica no banco e não no `limitador` em memória porque o
    limitador zera a cada restart do processo — e um servidor que reinicia sozinho é
    exatamente o cenário em que alguém quer o teto valendo.
    """
    with cursor() as c:
        r = c.execute(
            "SELECT COUNT(*) AS n FROM veiculos WHERE consultado_em >= ?", (desde_iso,)
        ).fetchone()
        return r["n"] if r else 0


def veiculos_stats() -> dict:
    """{total, ok, inexistentes, consultas} — quanto o cache guarda e quanto se pagou.

    Quem multiplica `consultas` pelo preço é o painel: preço é configuração
    (`apiplacas_custo_consulta`), não pertence à camada de dados.
    """
    with cursor() as c:
        r = c.execute(
            "SELECT COUNT(*) AS total, "
            "       SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok, "
            "       SUM(CASE WHEN status = 'inexistente' THEN 1 ELSE 0 END) AS inexistentes, "
            "       COALESCE(SUM(consultas), 0) AS consultas "
            "FROM veiculos"
        ).fetchone()
        return {
            "total": r["total"] or 0,
            "ok": r["ok"] or 0,
            "inexistentes": r["inexistentes"] or 0,
            "consultas": r["consultas"] or 0,
        }
