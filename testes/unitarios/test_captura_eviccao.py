"""Teto e evicção da coleta de dataset — por que ela parou 12 dias e o que a religa.

Medido em 25/08/2026, no posto:

    arquivos do `captura_dataset` (`-amostra`/`-naolido`) ....  4.184
    HISTÓRICO de `leitura.py`/`pipeline.py` (`{ts}_{placa}.jpg`)  1.594
    total que o teto contava ................................  5.784   (teto 5.000)

O teto contava os três subsistemas que escrevem em `app/web/static/snapshots/`, mas existe
para limitar só um. E o histórico cresce a cada leitura bem-sucedida e não pode ser apagado
sem quebrar `deteccoes.snapshot` — então o teto virava catraca: a coleta ficou desligada de
13/08 a 25/08, e nenhuma moto pôde ser coletada nesse período, o que travava a única
pendência que precisa de amostra nova.

Corrigir só a contagem não bastava: a captura produz **349 arquivos/hora** medidos, então
as 816 vagas que a correção liberaria dão 2,3 horas. Daí a evicção — e ela só é segura
porque alcança exclusivamente os arquivos que o próprio módulo criou.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from app.visao import captura_dataset as cap_mod
from app.visao.captura_dataset import CapturaDataset

IMG = np.zeros((40, 120, 3), dtype=np.uint8)


@pytest.fixture
def pasta(tmp_path, monkeypatch):
    """Isola `SNAPSHOT_DIR` e o `dataset.json` que a evicção consulta."""
    snaps = tmp_path / "snapshots"
    snaps.mkdir()
    monkeypatch.setattr(cap_mod, "SNAPSHOT_DIR", snaps)
    monkeypatch.chdir(tmp_path)          # `rotulos.protegidos` lê "testes/dataset.json" relativo
    (tmp_path / "testes").mkdir()
    (tmp_path / "testes" / "dataset.json").write_text('{"fotos": []}', encoding="utf-8")
    return snaps


def _cria(pasta, nome: str) -> None:
    (pasta / nome).write_bytes(b"jpg")


def _coletor(teto=10, **cfg) -> CapturaDataset:
    base = {"captura_dataset": "sim", "captura_dataset_negativos": "sim",
            "captura_dataset_max_arquivos": str(teto),
            "captura_dataset_intervalo_seg": "0",
            "captura_dataset_negativo_intervalo_seg": "0",
            "captura_dataset_moto_intervalo_seg": "0"}
    return CapturaDataset({**base, **cfg}, camera_db_id=3)


class TestTetoContaSoOQueEDele:
    def test_historico_nao_consome_a_cota_da_coleta(self, pasta):
        """O bug que desligou a coleta por 12 dias.

        Enche a pasta com o DOBRO do teto em arquivos de histórico e nenhum arquivo da
        coleta. A coleta tem de continuar liberada — o histórico não é dela.
        """
        for i in range(20):
            _cria(pasta, "20260813T12%04d_ABC1D23.jpg" % i)
            _cria(pasta, "20260813T12%04d_ABC1D23_frame.jpg" % i)
        c = _coletor(teto=10)
        assert c._cabe_no_disco() is True

    def test_arquivos_da_coleta_contam(self, pasta):
        """O outro lado: sem ele o teste acima passaria com o teto desligado."""
        for i in range(10):
            _cria(pasta, "20260813T12%04d_cam3-amostra.jpg" % i)
        c = _coletor(teto=10)
        # Cheio: só passa porque a evicção abriu vaga (ver a classe abaixo).
        assert c._cabe_no_disco() is True
        assert len(list(pasta.iterdir())) < 10, "devia ter evictado"

    def test_teto_zero_desliga_a_checagem(self, pasta):
        for i in range(50):
            _cria(pasta, "20260813T12%04d_cam3-amostra.jpg" % i)
        assert _coletor(teto=0)._cabe_no_disco() is True


class TestEviccao:
    def test_apaga_o_mais_antigo_e_continua_coletando(self, pasta):
        for i in range(12):
            _cria(pasta, "20260813T12%04d_cam3-amostra.jpg" % i)
        c = _coletor(teto=10)

        assert c._cabe_no_disco() is True
        restantes = sorted(f.name for f in pasta.iterdir())
        assert "20260813T120000_cam3-amostra.jpg" not in restantes, "o mais antigo sobreviveu"
        assert "20260813T120011_cam3-amostra.jpg" in restantes, "apagou o mais novo"

    def test_nunca_apaga_historico(self, pasta):
        """A razão pela qual o autor original preferiu PARAR a apagar — o risco era real."""
        for i in range(12):
            _cria(pasta, "20260813T12%04d_cam3-amostra.jpg" % i)
        _cria(pasta, "20260101T000000_HIST0R1.jpg")
        _cria(pasta, "20260101T000000_HIST0R1_frame.jpg")

        _coletor(teto=10)._cabe_no_disco()

        nomes = {f.name for f in pasta.iterdir()}
        assert "20260101T000000_HIST0R1.jpg" in nomes
        assert "20260101T000000_HIST0R1_frame.jpg" in nomes

    def test_nunca_apaga_captura_ja_rotulada(self, pasta, tmp_path):
        """Rótulo humano é a coisa mais caro de reproduzir aqui.

        Apagar uma captura rotulada transformaria trabalho de gente numa linha do dataset
        apontando para arquivo inexistente — modo de falha que o commit 2252896 já corrigiu
        uma vez.
        """
        for i in range(12):
            _cria(pasta, "20260813T12%04d_cam3-amostra.jpg" % i)
        rotulada = "20260813T120000_cam3-amostra.jpg"      # justamente a mais antiga
        (tmp_path / "testes" / "dataset.json").write_text(
            json.dumps({"fotos": [{"arquivo": "app/web/static/snapshots/" + rotulada,
                                   "placa_correta": "ABC1D23"}]}), encoding="utf-8")

        _coletor(teto=10)._cabe_no_disco()
        assert (pasta / rotulada).exists(), "apagou captura rotulada"

    def test_dataset_ilegivel_aborta_a_eviccao(self, pasta, tmp_path):
        """Não apagar é recuperável; apagar rótulo não. Na dúvida, para — como antes."""
        for i in range(12):
            _cria(pasta, "20260813T12%04d_cam3-amostra.jpg" % i)
        (tmp_path / "testes" / "dataset.json").write_text("{ não é json", encoding="utf-8")

        c = _coletor(teto=10)
        assert c._cabe_no_disco() is False
        assert len(list(pasta.iterdir())) == 12, "apagou com o dataset ilegível"

    def test_dataset_ausente_nao_bloqueia(self, pasta, tmp_path):
        """Sem dataset não há rótulo a proteger — diferente de dataset corrompido."""
        (tmp_path / "testes" / "dataset.json").unlink()
        for i in range(12):
            _cria(pasta, "20260813T12%04d_cam3-amostra.jpg" % i)
        assert _coletor(teto=10)._cabe_no_disco() is True

    def test_tudo_rotulado_para_em_vez_de_apagar(self, pasta, tmp_path):
        nomes = ["20260813T12%04d_cam3-amostra.jpg" % i for i in range(12)]
        for n in nomes:
            _cria(pasta, n)
        (tmp_path / "testes" / "dataset.json").write_text(
            json.dumps({"fotos": [{"arquivo": "x/" + n, "placa_correta": "A"} for n in nomes]}),
            encoding="utf-8")

        c = _coletor(teto=10)
        assert c._cabe_no_disco() is False
        assert len(list(pasta.iterdir())) == 12


class TestCotaDeMoto:
    """1.045 negativos coletados em agosto/2026 e a revisão humana não achou UMA moto.

    Não era azar de amostra: havia UM relógio de negativo (20 s) para tudo, e com carro
    inundando o gatilho a moto — o caso raro — chegava quase sempre logo depois de ele
    disparar.
    """

    def test_moto_e_carro_nao_disputam_o_mesmo_relogio(self, pasta):
        c = _coletor(teto=100, captura_dataset_negativo_intervalo_seg="9999",
                     captura_dataset_moto_intervalo_seg="0")
        c.negativo(IMG, "carro")
        c.negativo(IMG, "moto")

        nomes = sorted(f.name for f in pasta.iterdir())
        assert any(n.endswith("-naolido.jpg") for n in nomes), "carro não gravou"
        assert any(n.endswith("-naolido-moto.jpg") for n in nomes), (
            "moto foi barrada pelo relógio do carro — é exatamente o bug"
        )

    def test_moto_tem_marca_propria_no_nome(self, pasta):
        """Achável por nome: sem isso a moto fica indistinguível na fila de 1.045."""
        _coletor(teto=100).negativo(IMG, "moto")
        assert [f.name for f in pasta.iterdir()][0].endswith("-naolido-moto.jpg")

    def test_tipo_desconhecido_usa_o_relogio_comum(self, pasta):
        """`None` é a MAIORIA (423 de 838 detecções do banco). Chutar 'moto' daria cota de
        caso raro para o caso comum."""
        c = _coletor(teto=100, captura_dataset_moto_intervalo_seg="9999")
        c.negativo(IMG, None)
        assert [f.name for f in pasta.iterdir()][0].endswith("-naolido.jpg")

    def test_chamada_antiga_sem_tipo_continua_funcionando(self, pasta):
        """`negativo(crop)` sem o 2º argumento — o contrato que o pipeline usava."""
        _coletor(teto=100).negativo(IMG)
        assert len(list(pasta.iterdir())) == 1
