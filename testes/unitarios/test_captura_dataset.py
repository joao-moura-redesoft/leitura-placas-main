"""Captura de imagens para o dataset — o que o histórico descarta.

O pipeline só grava snapshot quando a leitura dá certo, então a base cresce contendo
apenas o que o sistema já acerta. O operador revisou 74 capturas automáticas em
12/08/2026 e não achou UMA moto, enquanto o dataset seguia com 2 — não é azar, é o
gatilho: placa de moto que não é detectada, ou é detectada e não é lida, nunca vira
arquivo.

Estes testes fixam os dois gatilhos novos (amostra periódica e negativo), os limites que
impedem a coleta de encher o disco, e a garantia de que o nome gerado NUNCA é lido como
placa — se fosse, a fila de classificação sugeriria uma placa inventada pelo nome do
arquivo, que é exatamente o tipo de rótulo falso que corrompe a medição.
"""
from __future__ import annotations

import threading

import numpy as np
import pytest

from app.visao.captura_dataset import CapturaDataset
from app.web.testes import _placa_do_nome

IMG = np.zeros((80, 200, 3), dtype=np.uint8)


@pytest.fixture
def dir_snap(tmp_path, monkeypatch):
    import app.visao.captura_dataset as mod
    monkeypatch.setattr(mod, "SNAPSHOT_DIR", tmp_path)
    return tmp_path


def _cap(dir_snap, **cfg):
    base = {"captura_dataset": "sim", "captura_dataset_intervalo_seg": "0",
            "captura_dataset_negativo_intervalo_seg": "0"}
    base.update(cfg)
    return CapturaDataset(base, camera_db_id=3)


def _arquivos(d):
    return sorted(f.name for f in d.iterdir())


def test_desligado_por_padrao(dir_snap):
    """Coletar custa disco e enche a fila — só liga quem quer montar base."""
    c = CapturaDataset({}, camera_db_id=3)
    c.amostrar(IMG)
    c.negativo(IMG)
    assert _arquivos(dir_snap) == []


def test_amostra_grava_o_quadro(dir_snap):
    _cap(dir_snap).amostrar(IMG)
    assert len(_arquivos(dir_snap)) == 1
    assert "-amostra." in _arquivos(dir_snap)[0]


def test_negativo_grava_o_recorte(dir_snap):
    _cap(dir_snap).negativo(IMG)
    assert "-naolido." in _arquivos(dir_snap)[0]


@pytest.mark.parametrize("marca", ["amostra", "naolido"])
def test_nome_nunca_e_lido_como_placa(dir_snap, marca):
    """A fila extrai `placa_sugerida` do nome do arquivo. Um nome que casasse com o
    padrão de placa faria a tela sugerir uma placa que ninguém leu."""
    c = _cap(dir_snap)
    c.amostrar(IMG) if marca == "amostra" else c.negativo(IMG)
    nome = _arquivos(dir_snap)[0]
    assert _placa_do_nome(nome) == ""


def test_intervalo_limita_a_amostragem(dir_snap):
    """Sem intervalo, a amostra gravaria a cada tick de detecção (varios por segundo)."""
    c = _cap(dir_snap, captura_dataset_intervalo_seg="3600")
    for _ in range(5):
        c.amostrar(IMG)
    assert len(_arquivos(dir_snap)) == 1


def test_negativo_tem_intervalo_proprio(dir_snap):
    """Caixa fantasma fixa na cena (adesivo, placa de sinalizacao) gravaria sem parar."""
    c = _cap(dir_snap, captura_dataset_negativo_intervalo_seg="3600")
    for _ in range(5):
        c.negativo(IMG)
    assert len(_arquivos(dir_snap)) == 1


def test_negativo_pode_ser_desligado_sem_desligar_a_amostra(dir_snap):
    c = _cap(dir_snap, captura_dataset_negativos="nao")
    c.negativo(IMG)
    assert _arquivos(dir_snap) == []
    c.amostrar(IMG)
    assert len(_arquivos(dir_snap)) == 1


def test_teto_ignora_arquivo_que_nao_e_da_coleta(dir_snap):
    """Arquivo de OUTRO subsistema não consome a cota da coleta.

    Este teste dizia "ao bater o teto a coleta PARA. Não apaga: apagar arriscaria remover
    snapshot que uma detecção do histórico referencia" — e o raciocínio estava certo. O que
    mudou em 25/08/2026 foi que o risco sumiu: a evicção só alcança `SUFIXOS_MEUS`, que
    nunca aparecem em `deteccoes.snapshot`.

    Parar custou caro: `leitura.py` e `pipeline.py` gravam o histórico na MESMA pasta, o
    teto contava tudo, e como o histórico cresce a cada leitura e não pode ser apagado, a
    coleta ficou 12 dias desligada (13/08 a 25/08). Medido no posto: 4.184 arquivos da
    coleta contra 1.594 de histórico, num teto de 5.000.
    """
    for i in range(3):
        (dir_snap / f"20260101T00000{i}_ABC1D23.jpg").write_bytes(b"x")   # histórico
    c = _cap(dir_snap, captura_dataset_max_arquivos="3")

    c.amostrar(IMG)

    # Gravou: o teto de 3 vale para os arquivos DELA, e ela ainda não tem nenhum.
    assert len(_arquivos(dir_snap)) == 4
    assert any(n.endswith("-amostra.jpg") for n in _arquivos(dir_snap))


def test_teto_cheio_de_arquivo_proprio_evicta_e_continua(dir_snap):
    """Cheio do que é dela: apaga o mais antigo e SEGUE coletando.

    O oposto do teste acima, e sem ele aquele passaria com o teto desligado.
    """
    for i in range(3):
        (dir_snap / f"20260101T00000{i}_cam9-amostra.jpg").write_bytes(b"x")
    c = _cap(dir_snap, captura_dataset_max_arquivos="3")

    c.amostrar(IMG)

    nomes = set(_arquivos(dir_snap))
    assert "20260101T000000_cam9-amostra.jpg" not in nomes, "o mais antigo sobreviveu"
    assert len(nomes) <= 3, "evicção não respeitou o teto"


def test_teto_zero_significa_sem_limite(dir_snap):
    c = _cap(dir_snap, captura_dataset_max_arquivos="0")
    c.amostrar(IMG)
    assert len(_arquivos(dir_snap)) == 1


def test_falha_de_escrita_nao_derruba_o_pipeline(dir_snap, monkeypatch):
    """Isto é coleta acessória: nunca pode interromper a operação da câmera."""
    import app.visao.captura_dataset as mod
    # Falha o PONTO DE ESCRITA de verdade, não `cv2.imwrite`: a gravação passou a ser
    # atômica (`arquivos.imwrite_atomico`, encode em memória + os.replace) e o teste
    # continuava verde patchando um `cv2.imwrite` que ninguém mais chamava — ou seja,
    # mediria "não levanta" de um caminho que nunca falhava. (Auditoria 05/09/2026.)
    monkeypatch.setattr(mod.arquivos, "escrever_bytes_atomico",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disco cheio")))

    _cap(dir_snap).amostrar(IMG)             # não levanta

    assert _arquivos(dir_snap) == []


def test_crop_vazio_e_ignorado(dir_snap):
    _cap(dir_snap).negativo(np.zeros((0, 0, 3), dtype=np.uint8))
    assert _arquivos(dir_snap) == []


class TestColetorNaoDisputaCameraComOPipeline:
    """O coletor autônomo (`_coletar_de_camera`) abre a câmera por conta própria.

    Numa câmera que já roda pipeline contínuo isso seria uma SEGUNDA conexão RTSP para o
    mesmo aparelho — a Intelbras só aceita uma —, além de coletar em duplicidade: o
    pipeline já chama `captura_dataset.amostrar()` de dentro do laço dele.

    O `lock_camera` não cobre isso: `Pipeline.iniciar()` só o segura durante
    `camera.abrir()` e segue com a conexão viva. Lock serializa abertura, não posse.
    """

    def _rodar_uma_volta(self, monkeypatch, pinst, dir_snap):
        """Executa um ciclo de `_coletar_de_camera` e diz se ele tentou abrir a câmera."""
        import app.visao.captura_dataset as mod
        import app.visao.pipeline as pipeline_mod

        if pinst is not None:
            monkeypatch.setitem(pipeline_mod._instancias, 55, pinst)
        abriu = []

        class _CameraFalsa:
            @staticmethod
            def capturar_frame_unico(**_kw):
                abriu.append(True)
                return IMG

        monkeypatch.setattr("app.visao.camera.capturar_frame_unico",
                            _CameraFalsa.capturar_frame_unico)
        monkeypatch.setattr("app.core.banco.cameras_obter",
                            lambda _id: {"camera_tipo": "rtsp", "ativo": 1,
                                         "camera_indice": "0"})
        monkeypatch.setattr("app.core.config.carregar",
                            lambda: {"captura_dataset": "sim", "captura_dataset_intervalo_seg": "0"})

        # Uma volta só: o `_parar.wait(intervalo)` devolve False na 1ª e True na 2ª.
        chamadas = {"n": 0}

        def _wait(_intervalo):
            chamadas["n"] += 1
            return chamadas["n"] > 1

        monkeypatch.setattr(mod._parar, "wait", _wait)
        mod._coletar_de_camera(55, 0.0)
        return bool(abriu)

    def test_pula_camera_com_pipeline_continuo(self, dir_snap, monkeypatch):
        assert self._rodar_uma_volta(
            monkeypatch, _PipelineFalso(automatica=True, viva=True), dir_snap) is False

    def test_coleta_quando_o_pipeline_falhou_ao_subir(self, dir_snap, monkeypatch):
        """`iniciar_camera` MANTÉM a instância registrada quando `iniciar()` levanta, para
        o supervisor tentar de novo. Nesse estado `_processar_frame` nunca roda: ninguém
        amostra e ninguém detém a conexão. Pular por "existe instância" zeraria a coleta
        exatamente na câmera com problema — e em silêncio, porque o log é DEBUG."""
        assert self._rodar_uma_volta(
            monkeypatch, _PipelineFalso(automatica=True, viva=False), dir_snap) is True

    def test_coleta_quando_a_camera_esta_em_modo_manual(self, dir_snap, monkeypatch):
        """Modo manual não mantém RTSP aberto (`_loop` só dorme) — aqui o coletor É a
        única fonte de imagem, e pular seria perder a coleta inteira."""
        assert self._rodar_uma_volta(
            monkeypatch, _PipelineFalso(automatica=False, viva=True), dir_snap) is True

    def test_coleta_quando_nao_ha_pipeline_algum(self, dir_snap, monkeypatch):
        assert self._rodar_uma_volta(monkeypatch, None, dir_snap) is True


class TestIniciarColetorConcorrente:
    """Achado do review de 28/08/2026: duas chamadas de `iniciar_coletor` rodando ao
    mesmo tempo (dois saves rápidos da tela de config, cada um numa thread de fundo
    própria desde o achado A6) podiam intercalar `_parar.set()`/`_coletores.clear()` de
    uma com as threads recém-criadas da outra. `_ciclo_lock` serializa o ciclo inteiro."""

    def test_chamadas_concorrentes_nao_deixam_estado_inconsistente(self, ambiente, posto, monkeypatch):
        import app.visao.captura_dataset as mod
        from app.core import config

        def _coletor_fake(cam_id, intervalo):
            while not mod._parar.wait(0.01):
                pass

        monkeypatch.setattr(mod, "_coletar_de_camera", _coletor_fake)
        cfg = {**config.carregar(), "captura_dataset": "sim",
              "captura_dataset_intervalo_seg": "60"}
        erros = []

        def chamar():
            try:
                mod.iniciar_coletor(cfg)
            except Exception as e:
                erros.append(e)

        try:
            threads_teste = [threading.Thread(target=chamar) for _ in range(10)]
            for t in threads_teste:
                t.start()
            for t in threads_teste:
                t.join(timeout=5)

            assert not erros, f"exceção durante a concorrência: {erros}"
            # Nenhuma thread órfã (já morta) escondida em `_coletores` — todas as que
            # sobraram são da geração que "venceu" e ainda estão de fato rodando.
            assert all(t.is_alive() for t in mod._coletores)
        finally:
            mod.parar_coletor()
            assert mod._coletores == []


class _PipelineFalso:
    """Dublê de Pipeline no que o coletor inspeciona: modo e se a thread está viva."""

    def __init__(self, automatica: bool, viva: bool):
        self.deteccao_automatica = automatica

        class _Thread:
            def is_alive(self_inner):
                return viva
        self._thread = _Thread()
