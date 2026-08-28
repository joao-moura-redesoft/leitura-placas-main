"""Teto de imagens do histórico: guardar foto só das N leituras mais recentes.

`retencao_dias` sozinho não segura disco — num posto movimentado deixa crescer 90 dias
antes de apagar a primeira coisa. Medido em 27/08/2026 nesta base: 971 leituras em 40 dias
ocupando 221 MB de imagem contra ~200 KB de banco. Daí a segunda política, que apaga o que
custa (a foto) e preserva o que não custa (a linha).

As duas coisas que este arquivo existe para travar:

1. A linha do histórico NÃO some. Placa, hora, bico e confiança continuam para auditoria e
   cobrança — a purga por prazo é que apaga tudo, e é outra política.
2. Foto rotulada em `testes/dataset.json` nunca é apagada. A pasta de snapshots é
   gitignored e não tem cópia; rótulo humano perdido não volta.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.core import banco, rotulos
from app.operacao import retencao as ret_mod


def _ts(dias_atras: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=dias_atras)).isoformat()


@pytest.fixture
def snaps(tmp_path, monkeypatch):
    """Isola a pasta que a purga apaga e o `dataset.json` que ela consulta.

    `_arquivo_de_url` resolve contra "app/web/static" RELATIVO ao diretório de trabalho,
    então trocar de cwd é o que isola de verdade — mexer só no `dataset.json` deixaria a
    purga apagando na pasta real da máquina.
    """
    monkeypatch.chdir(tmp_path)
    pasta = tmp_path / "app" / "web" / "static" / "snapshots"
    pasta.mkdir(parents=True)
    (tmp_path / "testes").mkdir()
    (tmp_path / "testes" / "dataset.json").write_text('{"fotos": []}', encoding="utf-8")
    return pasta


def _leitura(placa: str, dias_atras: float, pasta, com_frame: bool = True) -> int:
    """Uma detecção com os arquivos dela de fato em disco."""
    nome = f"{placa}.jpg"
    (pasta / nome).write_bytes(b"jpg")
    frame = None
    if com_frame:
        (pasta / f"{placa}_frame.jpg").write_bytes(b"jpg")
        frame = f"/static/snapshots/{placa}_frame.jpg"
    id_ = banco.registrar_deteccao(placa, "mercosul", 0.9,
                                   snapshot=f"/static/snapshots/{nome}", frame=frame)
    with banco.cursor() as c:
        c.execute("UPDATE deteccoes SET criado_em=? WHERE id=?", (_ts(dias_atras), id_))
    return id_


def _rotular(tmp_path, *nomes: str) -> None:
    fotos = [{"arquivo": f"app/web/static/snapshots/{n}", "placa": "X"} for n in nomes]
    (tmp_path / "testes" / "dataset.json").write_text(
        json.dumps({"fotos": fotos}), encoding="utf-8")


class TestQuemPerdeAFoto:
    def test_so_as_mais_antigas_alem_do_teto(self, ambiente, snaps):
        """Teto 3 com 5 leituras: as 2 mais antigas perdem a foto, as 3 novas ficam.

        O esperado é escrito à mão de propósito. Montá-lo reexecutando o mesmo ORDER BY da
        implementação faria o teste passar com QUALQUER ordenação — inclusive a invertida,
        que apagaria a foto das leituras recentes e guardaria a das velhas.
        """
        _leitura("AAA1A11", 5, snaps)    # mais antiga
        _leitura("BBB2B22", 4, snaps)
        _leitura("CCC3C33", 3, snaps)
        _leitura("DDD4D44", 2, snaps)
        _leitura("EEE5E55", 1, snaps)    # mais nova

        resultado = banco.imagens_excedentes(3)

        assert resultado["leituras_afetadas"] == 2
        com_foto = {d["placa"] for d in banco.listar_deteccoes() if d["snapshot"]}
        assert com_foto == {"CCC3C33", "DDD4D44", "EEE5E55"}
        sem_foto = {d["placa"] for d in banco.listar_deteccoes() if not d["snapshot"]}
        assert sem_foto == {"AAA1A11", "BBB2B22"}

    def test_a_linha_continua_no_historico(self, ambiente, snaps):
        """A diferença central para a purga por prazo: aqui a leitura NÃO some."""
        _leitura("AAA1A11", 5, snaps)
        _leitura("BBB2B22", 1, snaps)

        banco.imagens_excedentes(1)

        linhas = banco.listar_deteccoes()
        assert len(linhas) == 2
        velha = next(d for d in linhas if d["placa"] == "AAA1A11")
        assert velha["snapshot"] is None and velha["frame"] is None
        # O que a auditoria precisa continua lá:
        assert velha["confianca"] == 0.9 and velha["criado_em"]

    def test_apaga_recorte_e_quadro_da_mesma_leitura(self, ambiente, snaps):
        _leitura("AAA1A11", 5, snaps)
        _leitura("BBB2B22", 1, snaps)

        resultado = banco.imagens_excedentes(1)

        assert sorted(resultado["arquivos"]) == [
            "/static/snapshots/AAA1A11.jpg",
            "/static/snapshots/AAA1A11_frame.jpg",
        ]

    def test_leitura_sem_foto_nao_ocupa_vaga(self, ambiente, snaps):
        """O teto conta leituras COM imagem. Uma linha já sem foto (snapshot desligado,
        ou purgada numa passada anterior) não pode empurrar uma leitura com foto para
        fora — senão a purga anterior encolheria o teto efetivo a cada rodada."""
        banco.registrar_deteccao("SEMFOTO", "mercosul", 0.9)   # sem snapshot nenhum
        _leitura("AAA1A11", 5, snaps)
        _leitura("BBB2B22", 1, snaps)

        assert banco.imagens_excedentes(2)["leituras_afetadas"] == 0

    def test_teto_zero_nao_faz_nada(self, ambiente, snaps):
        _leitura("AAA1A11", 5, snaps)
        assert banco.imagens_excedentes(0) == {"arquivos": [], "leituras_afetadas": 0}
        assert banco.listar_deteccoes()[0]["snapshot"] is not None

    def test_ordena_igual_a_tela(self, ambiente, snaps):
        """Empate de `criado_em` desempata por id DESC, exatamente como `listar_deteccoes`.

        Duas câmeras no mesmo pulso gravam com o mesmo timestamp. Se a purga desempatasse
        diferente da listagem, ela comeria uma linha visível na página 1 e pouparia outra
        no fim do histórico.
        """
        mesmo = _ts(1)
        ids = [_leitura(p, 1, snaps) for p in ("AAA1A11", "BBB2B22", "CCC3C33")]
        with banco.cursor() as c:
            c.execute("UPDATE deteccoes SET criado_em=?", (mesmo,))

        banco.imagens_excedentes(2)

        # id maior = inserida depois = mais recente: sobrevivem os dois últimos.
        sem_foto = [d["id"] for d in banco.listar_deteccoes() if not d["snapshot"]]
        assert sem_foto == [ids[0]]


class TestProtecaoDeRotulo:
    def test_foto_rotulada_sai_do_historico_mas_fica_em_disco(self, ambiente, snaps, tmp_path):
        """O ponto de todo o mecanismo: a partir daqui o arquivo não é mais registro de
        operação, é insumo de dataset — e o dataset ainda aponta para ele."""
        _leitura("AAA1A11", 5, snaps)
        _leitura("BBB2B22", 1, snaps)
        _rotular(tmp_path, "AAA1A11.jpg")

        ret_mod.retencao._max_imagens = 1
        ret_mod.retencao._purgar_por_contagem()

        assert (snaps / "AAA1A11.jpg").exists()          # poupado por rótulo
        assert not (snaps / "AAA1A11_frame.jpg").exists()  # não rotulado: some
        assert banco.listar_deteccoes()[-1]["snapshot"] is None

    def test_abaixo_do_teto_nao_le_o_dataset_do_disco(self, ambiente, snaps, tmp_path, monkeypatch):
        """Achado do review de 28/08/2026: `_purgar_por_contagem` roda a cada 5 minutos —
        em regime estável (nada a purgar), não pode pagar o custo de ler e parsear
        `testes/dataset.json` só para descobrir que não havia nada a fazer."""
        _leitura("AAA1A11", 1, snaps)
        chamou = []
        monkeypatch.setattr(rotulos, "protegidos", lambda: (chamou.append(1), set())[1])

        ret_mod.retencao._max_imagens = 5   # bem acima do total de leituras com foto
        ret_mod.retencao._purgar_por_contagem()

        assert chamou == [], "não pode consultar rotulos.protegidos() sem nada a purgar"
        assert banco.listar_deteccoes()[0]["snapshot"] is not None, "nada devia ter sido tocado"

    def test_dataset_ilegivel_aborta_tudo(self, ambiente, snaps, tmp_path, monkeypatch):
        """Nem banco, nem disco. Se anulasse a coluna e só então falhasse em apagar, o
        arquivo viraria órfão — invisível para toda limpeza futura, porque todas partem
        do banco."""
        _leitura("AAA1A11", 5, snaps)
        _leitura("BBB2B22", 1, snaps)
        (tmp_path / "testes" / "dataset.json").write_text("{ nao e json", encoding="utf-8")
        assert rotulos.protegidos() is None, "pré-condição: o dataset tem de estar ilegível"

        ret_mod.retencao._max_imagens = 1
        ret_mod.retencao._purgar_por_contagem()

        assert (snaps / "AAA1A11.jpg").exists()
        assert all(d["snapshot"] for d in banco.listar_deteccoes())

    def test_dataset_ausente_nao_impede_a_purga(self, ambiente, snaps, tmp_path):
        """Ausente é diferente de ilegível: não há rótulo a proteger, então a purga roda
        (com aviso). Tratar os dois igual travaria a limpeza em toda instalação nova."""
        _leitura("AAA1A11", 5, snaps)
        _leitura("BBB2B22", 1, snaps)
        (tmp_path / "testes" / "dataset.json").unlink()

        ret_mod.retencao._max_imagens = 1
        ret_mod.retencao._purgar_por_contagem()

        assert not (snaps / "AAA1A11.jpg").exists()


class TestOrfaos:
    def test_remover_deteccao_leva_os_arquivos(self, ambiente, snaps):
        """Antes a linha sumia e o JPEG ficava para sempre — e órfão é invisível para o
        teto de contagem, que parte do banco."""
        id_ = _leitura("AAA1A11", 1, snaps)

        arquivos = banco.remover_deteccao(id_)

        assert sorted(arquivos) == ["/static/snapshots/AAA1A11.jpg",
                                    "/static/snapshots/AAA1A11_frame.jpg"]
        assert ret_mod.apagar_orfaos(arquivos) == 2
        assert not (snaps / "AAA1A11.jpg").exists()

    def test_rota_delete_preserva_snapshot_rotulado(self, admin, ambiente, snaps, tmp_path):
        """Achado A3: apagar uma detecção HISTÓRICA pelo admin não pode destruir um
        snapshot que já virou insumo de dataset — ao contrário da absorção do pipeline
        (janela curta de ~120s), aqui não há nenhuma garantia de que o rótulo ainda não
        exista."""
        id_ = _leitura("AAA1A11", 1, snaps)
        _rotular(tmp_path, "AAA1A11.jpg")

        r = admin.delete(f"/api/deteccoes/{id_}")

        assert r.status_code == 200
        assert (snaps / "AAA1A11.jpg").exists(), "snapshot rotulado não pode ser apagado"
        assert banco.listar_deteccoes() == [], "a LINHA some normalmente"

    def test_id_inexistente_e_none_e_nao_lista_vazia(self, ambiente, snaps):
        """`None` (não existia) tem de ser distinguível de `[]` (existia, sem foto): a
        rota devolve 404 no primeiro caso, e `if not lista` colapsaria os dois."""
        assert banco.remover_deteccao(999) is None

    def test_deteccao_sem_foto_devolve_lista_vazia(self, ambiente, snaps):
        id_ = banco.registrar_deteccao("SEMFOTO", "mercosul", 0.9)
        assert banco.remover_deteccao(id_) == []

    def test_rota_delete_nao_da_404_em_leitura_sem_foto(self, admin, ambiente, snaps):
        """Regressão do `if not banco.remover_deteccao(id_)`: a linha já tinha sido
        apagada quando a rota respondia 404."""
        id_ = banco.registrar_deteccao("SEMFOTO", "mercosul", 0.9)
        assert admin.delete(f"/api/deteccoes/{id_}").status_code == 200
        assert banco.listar_deteccoes() == []

    def test_apagar_orfaos_recusa_travessia(self, ambiente, snaps):
        """Mesma trava de `_arquivo_de_url`, agora no caminho que os chamadores usam."""
        assert ret_mod.apagar_orfaos(["/static/../../etc/passwd", "/outro/x.jpg"]) == 0


class TestHistoricoAguentaFotoNula:
    def test_payload_da_api_nao_quebra(self, admin, ambiente, snaps):
        """A tela lê `d.snapshot`/`d.frame` e cai para '—' quando os dois são nulos —
        mas só se o payload continuar chegando com as chaves."""
        _leitura("AAA1A11", 5, snaps)
        _leitura("BBB2B22", 1, snaps)
        banco.imagens_excedentes(1)

        linhas = admin.get("/api/deteccoes").json()
        velha = next(d for d in linhas if d["placa"] == "AAA1A11")
        assert velha["snapshot"] is None and velha["frame"] is None
        assert velha["placa"] == "AAA1A11"
