"""Duas câmeras no MESMO aparelho físico não podem derrubar o processo.

Incidente de campo (03/09/2026, máquina da feira). Duas linhas de cadastro apontavam para
a mesma webcam (índice USB 0). A `cam1` entrou em `reconectar()` — que faz `cap.release()`
— e 700 ms depois a `cam2` chamou `cv2.VideoCapture()` no mesmo dispositivo. O servidor
**morreu sem traceback Python**: access violation dentro do DSHOW.

O `leitura.lock_camera` não protegia isso porque ele é indexado por `camera_id`: dois ids
diferentes davam locks diferentes, embora o aparelho fosse um só.

Duas defesas, testadas aqui:
  1. `camera._lock_do_dispositivo` — serializa abertura e liberação por IDENTIDADE DO
     APARELHO, tornando a corrida sobrevivível mesmo com o cadastro errado.
  2. `api._recusar_usb_duplicada` — barra o cadastro que não faz sentido nenhum (duas
     câmeras lendo exatamente a mesma imagem), evitando o problema em vez de administrá-lo.
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi import HTTPException

from app.visao import camera as cm
from app.web import api as api_rotas


class TestChaveDoDispositivo:
    def test_mesmo_indice_usb_e_o_mesmo_aparelho(self):
        a = cm.Camera(tipo="usb", indice="0")
        b = cm.Camera(tipo="usb", indice="0")
        assert a._chave_dispositivo() == b._chave_dispositivo()
        assert cm._lock_do_dispositivo(a._chave_dispositivo()) is \
               cm._lock_do_dispositivo(b._chave_dispositivo())

    def test_indices_diferentes_nao_se_bloqueiam(self):
        """Duas webcams de verdade têm de rodar em paralelo — o lock não pode ser global."""
        a = cm.Camera(tipo="usb", indice="0")
        b = cm.Camera(tipo="usb", indice="1")
        assert cm._lock_do_dispositivo(a._chave_dispositivo()) is not \
               cm._lock_do_dispositivo(b._chave_dispositivo())

    def test_indice_vazio_ou_invalido_cai_no_zero(self):
        """`camera_indice` chega como texto do formulário; vazio é o caso comum."""
        assert cm.Camera(tipo="usb", indice="")._chave_dispositivo() == "usb:0"

    def test_rtsp_e_chaveado_pela_url(self):
        r1 = cm.Camera(tipo="rtsp", indice="", intelbras={"host": "10.0.0.1"})
        r2 = cm.Camera(tipo="rtsp", indice="", intelbras={"host": "10.0.0.2"})
        assert r1._chave_dispositivo() != r2._chave_dispositivo()

    def test_mesma_url_rtsp_compartilha_lock(self):
        """Duas linhas apontando para o MESMO stream também disputam um recurso só."""
        r1 = cm.Camera(tipo="rtsp", indice="", intelbras={"host": "10.0.0.1", "canal": "3"})
        r2 = cm.Camera(tipo="rtsp", indice="", intelbras={"host": "10.0.0.1", "canal": "3"})
        assert r1._chave_dispositivo() == r2._chave_dispositivo()


class TestSerializacao:
    def test_abertura_no_mesmo_aparelho_nao_e_concorrente(self, monkeypatch):
        """A corrida do incidente: uma instância abrindo enquanto a outra abre.

        Mede SOBREPOSIÇÃO, não ordem: o que mata o processo é as duas estarem dentro do
        código nativo ao mesmo tempo.
        """
        dentro = []
        sobrepos = []

        def falso_abrir(self):
            dentro.append(1)
            if len(dentro) > 1:
                sobrepos.append(1)
            time.sleep(0.05)
            dentro.pop()

        monkeypatch.setattr(cm.Camera, "_abrir_sem_lock", falso_abrir)
        cams = [cm.Camera(tipo="usb", indice="0") for _ in range(4)]
        ts = [threading.Thread(target=c.abrir) for c in cams]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=5)
        assert not sobrepos, "duas aberturas no mesmo aparelho se sobrepuseram"

    def test_aparelhos_distintos_correm_em_paralelo(self, monkeypatch):
        """O lock não pode custar o paralelismo entre câmeras de verdade."""
        ordem = []

        def falso_abrir(self):
            ordem.append(("entrou", self.indice))
            time.sleep(0.15)
            ordem.append(("saiu", self.indice))

        monkeypatch.setattr(cm.Camera, "_abrir_sem_lock", falso_abrir)
        a, b = cm.Camera(tipo="usb", indice="0"), cm.Camera(tipo="usb", indice="1")
        ts = [threading.Thread(target=x.abrir) for x in (a, b)]
        t0 = time.time()
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=5)
        # Serializado levaria ~0,30 s; em paralelo, ~0,15 s.
        assert time.time() - t0 < 0.28, "aparelhos diferentes foram serializados"


class TestCadastroDuplicado:
    def _cam(self, **kw):
        base = {"nome": "cam", "empresa_id": 1, "camera_tipo": "usb", "camera_indice": "0"}
        return {**base, **kw}

    def test_recusa_segunda_camera_no_mesmo_indice(self, ambiente, monkeypatch):
        from app.core import banco
        monkeypatch.setattr(banco, "cameras_listar",
                            lambda *a, **k: [{"id": 1, "nome": "Webcam do estande",
                                              "camera_tipo": "usb", "camera_indice": "0"}])
        with pytest.raises(HTTPException) as e:
            api_rotas._recusar_usb_duplicada(self._cam())
        assert e.value.status_code == 409
        # A mensagem tem de dizer QUAL câmera colide e o que fazer — senão o operador
        # só sabe que "deu erro".
        assert "Webcam do estande" in e.value.detail

    def test_outro_indice_passa(self, ambiente, monkeypatch):
        from app.core import banco
        monkeypatch.setattr(banco, "cameras_listar",
                            lambda *a, **k: [{"id": 1, "nome": "x", "camera_tipo": "usb",
                                              "camera_indice": "0"}])
        api_rotas._recusar_usb_duplicada(self._cam(camera_indice="1"))

    def test_editar_a_propria_camera_nao_colide_consigo(self, ambiente, monkeypatch):
        """Sem o `id_atual`, salvar o nome de uma câmera recusaria a si mesma."""
        from app.core import banco
        monkeypatch.setattr(banco, "cameras_listar",
                            lambda *a, **k: [{"id": 7, "nome": "x", "camera_tipo": "usb",
                                              "camera_indice": "0"}])
        api_rotas._recusar_usb_duplicada(self._cam(), id_atual=7)

    def test_rtsp_nao_e_barrado(self, ambiente, monkeypatch):
        """Dois bicos podem legitimamente usar o mesmo DVR — a regra é só para USB/CSI."""
        from app.core import banco
        monkeypatch.setattr(banco, "cameras_listar",
                            lambda *a, **k: [{"id": 1, "nome": "dvr", "camera_tipo": "rtsp",
                                              "camera_indice": "0"}])
        api_rotas._recusar_usb_duplicada(self._cam(camera_tipo="rtsp"))
