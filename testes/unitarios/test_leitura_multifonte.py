"""Leitura com DUAS câmeras por bico: revezamento, regra adaptativa e ordem dos locks.

A segunda câmera existe porque a câmera do posto fica elevada: com estepe/roda na
traseira, a placa traseira não aparece em pixel nenhum e nenhum ajuste de OCR resolve.
Mas duas câmeras dividem UM orçamento de tempo — então o ganho só é real se o laço
souber parar de gastar fotos na câmera que não está enxergando nada. É essa regra
(`_revisar_fontes`) que decide se a feature ajuda ou atrapalha, e ela é testada aqui
isolada: sem câmera, sem modelo, sem frame.
"""
from __future__ import annotations

import threading

import numpy as np
import pytest

from app.visao import leitura as leitura_mod
from app.visao.leitura import FonteLeitura, _adquirir_locks, _revisar_fontes

from test_payload_leitura import (CFG, BICO_ID, PLACA, _DetectorFalso, _OcrFalso,
                                  _especificacao, _provedor_de_frames, visao_falsa)  # noqa: F401


def _fonte(camera_id: int, papel: str = "traseira", bboxes: int = 0, tentativas: int = 0,
           ativa: bool = True) -> FonteLeitura:
    return FonteLeitura(camera_id=camera_id, papel=papel, especificacao=None, roi=None,
                        bboxes=bboxes, tentativas=tentativas, ativa=ativa)


class TestRegraAdaptativa:
    """`_revisar_fontes` é função pura sobre a lista — o núcleo da decisão."""

    def test_abandona_a_camera_cega_a_partir_da_rodada_minima(self):
        """O caso que motivou a feature: traseira bloqueada pelo estepe. Sem abandoná-la,
        metade do orçamento iria para uma câmera que nunca devolveria nada."""
        cega, boa = _fonte(1, "traseira", bboxes=0, tentativas=2), _fonte(2, "frente", bboxes=3)
        _revisar_fontes([cega, boa], rodada=2)
        assert cega.ativa is False
        assert "sem detecção" in cega.motivo_inativa
        assert boa.ativa is True

    def test_nao_abandona_antes_da_rodada_minima(self):
        """Uma rodada é pouco: um único frame borrado mataria a câmera boa."""
        cega, boa = _fonte(1, bboxes=0), _fonte(2, bboxes=3)
        _revisar_fontes([cega, boa], rodada=1)
        assert cega.ativa is True

    def test_todas_em_zero_mantem_todas(self):
        """Sem nenhuma detectando não há como discriminar — desligar uma seria chute, e
        o carro pode simplesmente ainda não ter chegado na área."""
        a, b = _fonte(1, bboxes=0), _fonte(2, bboxes=0)
        _revisar_fontes([a, b], rodada=5)
        assert (a.ativa, b.ativa) == (True, True)

    def test_nunca_abandona_a_ultima_fonte(self):
        """Bico de uma câmera: sem placa não há para onde migrar, e desistir cedo só
        devolveria "não li" mais rápido."""
        so_uma = _fonte(1, bboxes=0, tentativas=9)
        _revisar_fontes([so_uma], rodada=9)
        assert so_uma.ativa is True

    def test_nao_ressuscita_fonte_ja_abandonada(self):
        """Monotônica: reviver pagaria de novo o custo do revezamento contra evidência
        acumulada em contrário."""
        morta, boa = _fonte(1, bboxes=0, ativa=False), _fonte(2, bboxes=1)
        _revisar_fontes([morta, boa], rodada=3)
        assert morta.ativa is False

    def test_e_idempotente(self):
        cega, boa = _fonte(1, bboxes=0), _fonte(2, bboxes=2)
        _revisar_fontes([cega, boa], rodada=2)
        _revisar_fontes([cega, boa], rodada=3)
        assert (cega.ativa, boa.ativa) == (False, True)


class TestLockAntesDeAbrirConexao:
    """A câmera Intelbras aceita UMA conexão RTSP — quem abre conexão direta tem de
    estar com o lock daquela câmera na mão."""

    def _abrir_espiando(self, monkeypatch, fonte):
        """Roda a abertura substituindo a fase 2 por um espião do estado do lock."""
        visto = {}

        def _espiao(f, _cfg):
            visto["lock"] = f.lock
            visto["adquirido"] = f.lock_adquirido

        monkeypatch.setattr(leitura_mod, "_abrir_uma", _espiao)
        leitura_mod._abrir_fontes([fonte], CFG, espera_lock=1.0)
        leitura_mod._liberar_fontes([fonte])
        return visto

    def test_queda_do_pipeline_para_conexao_direta_toma_o_lock(self, monkeypatch):
        """Regressão: o lock era decidido por `provider is None`, ANTES de sondar o
        pipeline. Um provider que devolve None (pipeline reconectando/aquecendo) caía
        para conexão direta sem lock nenhum, permitindo uma segunda sessão RTSP
        simultânea na mesma câmera — que é o que derrubava a leitura."""
        f = leitura_mod.FonteLeitura(camera_id=77, papel="traseira", especificacao=None,
                                     roi=None, provider=lambda: None)
        visto = self._abrir_espiando(monkeypatch, f)
        assert f.usar_pipeline is False
        assert visto["lock"] is not None, "conexão direta sem lock — RTSP concorrente"
        assert visto["adquirido"] is True

    def test_sem_provider_tambem_toma_o_lock(self, monkeypatch):
        f = leitura_mod.FonteLeitura(camera_id=78, papel="frente", especificacao=None,
                                     roi=None, provider=None)
        visto = self._abrir_espiando(monkeypatch, f)
        assert visto["adquirido"] is True

    def test_fonte_servida_pelo_pipeline_nao_toma_o_lock(self, monkeypatch):
        """Contrapeso: reusar o frame do pipeline não abre conexão, e prender a câmera
        por toda a leitura (até 28s) travaria o coletor de dataset e o snapshot do editor
        de áreas sem motivo."""
        monkeypatch.setattr(leitura_mod, "_abrir_uma", lambda f, _c: None)
        f = leitura_mod.FonteLeitura(camera_id=79, papel="traseira", especificacao=None,
                                     roi=None, provider=_provedor_de_frames())
        leitura_mod._abrir_fontes([f], CFG, espera_lock=1.0)
        assert f.usar_pipeline is True
        assert f.lock is None and f.lock_adquirido is False
        leitura_mod._liberar_fontes([f])

    def test_provider_que_estoura_cai_para_direta_com_lock(self, monkeypatch):
        """O provider é código de rede: um erro dele significa "sem frame do pipeline",
        não leitura perdida — mas a conexão direta resultante precisa do lock."""
        def _provider_ruim():
            raise RuntimeError("pipeline morreu")

        f = leitura_mod.FonteLeitura(camera_id=80, papel="frente", especificacao=None,
                                     roi=None, provider=_provider_ruim)
        visto = self._abrir_espiando(monkeypatch, f)
        assert f.usar_pipeline is False
        assert visto["adquirido"] is True


class TestOrdemDosLocks:
    def test_adquire_sempre_na_ordem_crescente_de_camera_id(self):
        """Ordem total sobre o recurso — é o que elimina o ciclo de espera."""
        f2, f1 = _fonte(9), _fonte(4)
        for f in (f2, f1):
            f.lock = leitura_mod._obter_lock_camera(f.camera_id)
        _adquirir_locks([f2, f1], espera_seg=1.0)
        assert f1.lock_adquirido and f2.lock_adquirido
        leitura_mod._liberar_fontes([f1, f2])

    def test_dois_bicos_com_o_par_invertido_nao_travam(self):
        """Bico A = [3, 4] e bico B = [4, 3]: sob aquisição na ordem da lista, cada um
        segura o lock que o outro espera e os dois ficam parados para sempre."""
        leitura_mod._locks_camera.clear()
        pronto = []

        def leitura_de(ids):
            fontes = [_fonte(i) for i in ids]
            for f in fontes:
                f.lock = leitura_mod._obter_lock_camera(f.camera_id)
            _adquirir_locks(fontes, espera_seg=2.0)
            leitura_mod._liberar_fontes(fontes)
            pronto.append(tuple(ids))

        threads = [threading.Thread(target=leitura_de, args=(ids,))
                   for ids in ([3, 4], [4, 3])]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        assert not any(t.is_alive() for t in threads), "deadlock entre os dois bicos"
        assert len(pronto) == 2


class TestLeituraComDuasFontes:
    """Laço completo, com detector/OCR dublês (ver test_payload_leitura)."""

    def _ler(self, fontes, **kw):
        base = dict(fontes=fontes, cfg=CFG, preview_nome=f"preview_bico_{BICO_ID}",
                    bico_id=BICO_ID, origem="teste")
        return leitura_mod.ler_placa(**{**base, **kw})

    def _fonte_viva(self, camera_id, papel):
        return FonteLeitura(camera_id=camera_id, papel=papel,
                            especificacao=_especificacao(), roi=None,
                            provider=_provedor_de_frames())

    def test_as_duas_cameras_contribuem_e_o_payload_descreve_cada_uma(self, ambiente, visao_falsa):
        visao_falsa(_DetectorFalso(), _OcrFalso())
        r = self._ler([self._fonte_viva(7, "traseira"), self._fonte_viva(8, "frente")])

        assert r["placa"] == PLACA
        assert {f["camera_id"] for f in r["fontes"]} == {7, 8}
        assert {f["papel"] for f in r["fontes"]} == {"traseira", "frente"}
        # Revezamento: as duas tiraram foto, e o total bate com a soma.
        assert all(f["tentativas"] >= 1 for f in r["fontes"])
        assert sum(f["tentativas"] for f in r["fontes"]) == r["tentativas"]
        assert r["n_cameras_votando"] == 2

    def test_camera_vencedora_vai_para_o_historico(self, ambiente, visao_falsa):
        """`camera_db_id` errado faz a PRÓXIMA chamada cruzar o pipeline pela câmera
        errada e duplicar o veículo — por isso a origem do voto é rastreada."""
        from app.core import banco
        visao_falsa(_DetectorFalso(), _OcrFalso())
        r = self._ler([self._fonte_viva(7, "traseira"), self._fonte_viva(8, "frente")],
                      origem="roteador")
        gravada = banco.listar_deteccoes(origem="todas")[0]
        assert gravada["camera_db_id"] == r["camera_id"]
        assert r["camera_id"] in (7, 8)

    def test_uma_fonte_que_nao_abre_degrada_em_vez_de_derrubar(self, ambiente, visao_falsa):
        """Redundância que vira ponto de falha é o oposto do que a feature promete."""
        visao_falsa(_DetectorFalso(), _OcrFalso())
        viva = self._fonte_viva(7, "traseira")
        # Sem provider e sem câmera direta que abra: a fonte falha na abertura.
        morta = FonteLeitura(camera_id=8, papel="frente",
                             especificacao=_especificacao(), roi=None, provider=None)
        r = self._ler([viva, morta])

        assert r["placa"] == PLACA               # leu, apesar de perder uma câmera
        assert r["avisos"], "a câmera perdida tem que aparecer nos avisos"
        estados = {f["camera_id"]: f["estado"] for f in r["fontes"]}
        assert estados[7] == "usada" and estados[8] == "indisponivel"

    def test_todas_as_fontes_fora_e_erro(self, ambiente, visao_falsa):
        visao_falsa(_DetectorFalso(), _OcrFalso())
        mortas = [FonteLeitura(camera_id=c, papel=p, especificacao=_especificacao(),
                               roi=None, provider=None)
                  for c, p in ((7, "traseira"), (8, "frente"))]
        with pytest.raises(leitura_mod.LeituraError) as exc:
            self._ler(mortas)
        assert exc.value.status == 503

    def test_cada_fonte_recorta_pelo_proprio_roi(self, ambiente, visao_falsa):
        """O ROI está em coordenadas do frame de UMA câmera; aplicar o da traseira na
        imagem da frente recortaria a região errada, sem erro nenhum na hora."""
        vistos = []

        class _DetectorQueAnotaTamanho:
            def detectar(self, frame):
                vistos.append(frame.shape[:2])
                return [(10, 10, 60, 20, 0.9)]

        visao_falsa(_DetectorQueAnotaTamanho(), _OcrFalso())
        a = self._fonte_viva(7, "traseira")
        a.roi = {"x": 0, "y": 0, "w": 100, "h": 80}
        b = self._fonte_viva(8, "frente")
        b.roi = {"x": 0, "y": 0, "w": 200, "h": 160}
        self._ler([a, b])

        assert (80, 100) in vistos, "recorte da traseira não foi aplicado"
        assert (160, 200) in vistos, "recorte da frente não foi aplicado"


class TestPreviewPorCamera:
    def test_grava_um_preview_para_cada_camera(self, ambiente, visao_falsa):
        """O operador precisa conferir o enquadramento das DUAS — principalmente o da que
        não achou nada, que é onde está o problema a corrigir."""
        visao_falsa(_DetectorFalso(), _OcrFalso())
        fontes = [FonteLeitura(camera_id=c, papel=p, especificacao=_especificacao(),
                               roi=None, provider=_provedor_de_frames())
                  for c, p in ((7, "traseira"), (8, "frente"))]
        leitura_mod.ler_placa(fontes=fontes, cfg=CFG,
                              preview_nome=f"preview_bico_{BICO_ID}", bico_id=BICO_ID,
                              origem="teste")

        assert leitura_mod.caminho_preview_bico(BICO_ID).exists()          # canônico
        assert leitura_mod.caminho_preview_bico(BICO_ID, 7).exists()
        assert leitura_mod.caminho_preview_bico(BICO_ID, 8).exists()

    def test_bico_de_uma_camera_nao_gera_arquivo_extra(self, ambiente, visao_falsa):
        """Com uma fonte o canônico já é esse quadro — um segundo arquivo idêntico seria
        só lixo em disco."""
        visao_falsa(_DetectorFalso(), _OcrFalso())
        fonte = FonteLeitura(camera_id=7, papel="traseira", especificacao=_especificacao(),
                             roi=None, provider=_provedor_de_frames())
        leitura_mod.ler_placa(fontes=[fonte], cfg=CFG,
                              preview_nome=f"preview_bico_{BICO_ID}", bico_id=BICO_ID,
                              origem="teste")
        assert leitura_mod.caminho_preview_bico(BICO_ID).exists()
        assert not leitura_mod.caminho_preview_bico(BICO_ID, 7).exists()
