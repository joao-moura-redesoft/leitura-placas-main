"""Área de captura (ROI) do bico: ciclo de gravação/limpeza e o vínculo com a câmera.

O ROI é guardado em COORDENADAS DO FRAME da câmera do bico. Isso o torna a única parte
do cadastro que só faz sentido junto de uma câmera específica — e é o que motiva o teste
de troca de câmera aqui: um retângulo herdado da câmera anterior não dá erro nenhum,
simplesmente recorta o pedaço errado da imagem, e o sintoma aparece semanas depois como
"a leitura desse bico piorou".
"""
from __future__ import annotations

import json

import pytest

from app.core import banco

ROI = {"x": 10, "y": 20, "w": 300, "h": 200}


def _roi_do_bico(bico_id: int) -> dict | None:
    bruto = banco.bicos_obter(bico_id)["roi"]
    return json.loads(bruto) if bruto else None


@pytest.fixture
def outra_camera(admin, posto):
    """Segunda câmera do MESMO posto — destino válido para realocar o bico."""
    return admin.post("/api/cameras", json={
        "nome": "Cam 2", "empresa_id": posto["empresa_id"],
        "camera_tipo": "rtsp", "rtsp_url_custom": "rtsp://x/2",
    }).json()["id"]


class TestCicloDoRoi:
    def test_salva_e_depois_limpa(self, admin, posto):
        assert admin.put(f"/api/bicos/{posto['bico_id']}/roi", json=ROI).status_code == 200
        assert _roi_do_bico(posto["bico_id"]) == ROI

        assert admin.delete(f"/api/bicos/{posto['bico_id']}/roi").status_code == 200
        assert _roi_do_bico(posto["bico_id"]) is None

    def test_sobrescreve_o_anterior(self, admin, posto):
        admin.put(f"/api/bicos/{posto['bico_id']}/roi", json=ROI)
        novo = {"x": 0, "y": 0, "w": 50, "h": 60}
        admin.put(f"/api/bicos/{posto['bico_id']}/roi", json=novo)
        assert _roi_do_bico(posto["bico_id"]) == novo

    @pytest.mark.parametrize("payload", [
        {"x": 1, "y": 2, "w": 0, "h": 10},      # largura zero
        {"x": 1, "y": 2, "w": 10, "h": -5},     # altura negativa
        {"x": 1, "y": 2, "w": 10},              # falta h
        {"x": "abc", "y": 2, "w": 10, "h": 10},
    ])
    def test_recusa_retangulo_invalido(self, admin, posto, payload):
        """Um ROI degenerado não estoura na hora: `frame[y:y+h, x:x+w]` devolve um
        recorte vazio e a leitura simplesmente nunca acha placa nenhuma."""
        assert admin.put(f"/api/bicos/{posto['bico_id']}/roi", json=payload).status_code == 400
        assert _roi_do_bico(posto["bico_id"]) is None

    def test_bico_inexistente(self, admin):
        assert admin.put("/api/bicos/9999/roi", json=ROI).status_code == 404
        assert admin.delete("/api/bicos/9999/roi").status_code == 404


class TestRoiSegueACamera:
    """O ROI pertence ao par (bico, câmera) — trocar a câmera invalida o retângulo."""

    def test_trocar_a_camera_do_bico_limpa_o_roi(self, admin, posto, outra_camera):
        """Regressão: o UPDATE do bico mexia em `camera_id` sem tocar em `roi`, então o
        bico continuava recortando as coordenadas da câmera ANTIGA na imagem da nova —
        sem erro nenhum, só leitura pior."""
        admin.put(f"/api/bicos/{posto['bico_id']}/roi", json=ROI)
        assert _roi_do_bico(posto["bico_id"]) == ROI

        r = admin.put(f"/api/bicos/{posto['bico_id']}", json={
            "automacao_id": posto["automacao_id"], "codigo": "3", "camera_id": outra_camera})
        assert r.status_code == 200
        assert _roi_do_bico(posto["bico_id"]) is None

    def test_editar_o_bico_sem_trocar_a_camera_preserva_o_roi(self, admin, posto):
        """O contrapeso do teste acima: renomear/reordenar um bico não pode custar o
        trabalho de quem desenhou a área."""
        admin.put(f"/api/bicos/{posto['bico_id']}/roi", json=ROI)
        r = admin.put(f"/api/bicos/{posto['bico_id']}", json={
            "automacao_id": posto["automacao_id"], "codigo": "3",
            "camera_id": posto["camera_id"], "nome": "Bomba 1 esquerda", "bomba": 1, "lado": 1})
        assert r.status_code == 200
        assert _roi_do_bico(posto["bico_id"]) == ROI


class TestRoiPorCamera:
    """Bico de 2 câmeras tem 2 áreas independentes — cada uma em coordenadas da sua."""

    def _roi2(self, bico_id):
        bruto = banco.bicos_obter(bico_id)["roi2"]
        return json.loads(bruto) if bruto else None

    def test_sem_camera_id_grava_na_primeira(self, admin, posto_2cam):
        """Compatibilidade: todo chamador anterior à segunda câmera continua correto."""
        admin.put(f"/api/bicos/{posto_2cam['bico_id']}/roi", json=ROI)
        assert _roi_do_bico(posto_2cam["bico_id"]) == ROI
        assert self._roi2(posto_2cam["bico_id"]) is None

    def test_com_camera_id_grava_no_slot_certo(self, admin, posto_2cam):
        outra = {"x": 5, "y": 5, "w": 120, "h": 90}
        admin.put(f"/api/bicos/{posto_2cam['bico_id']}/roi", json=ROI)
        admin.put(f"/api/bicos/{posto_2cam['bico_id']}/roi",
                  json={**outra, "camera_id": posto_2cam["camera2_id"]})
        assert _roi_do_bico(posto_2cam["bico_id"]) == ROI       # slot 1 intacto
        assert self._roi2(posto_2cam["bico_id"]) == outra

    def test_delete_limpa_so_a_area_daquela_camera(self, admin, posto_2cam):
        admin.put(f"/api/bicos/{posto_2cam['bico_id']}/roi", json=ROI)
        admin.put(f"/api/bicos/{posto_2cam['bico_id']}/roi",
                  json={**ROI, "camera_id": posto_2cam["camera2_id"]})
        admin.delete(f"/api/bicos/{posto_2cam['bico_id']}/roi"
                     f"?camera_id={posto_2cam['camera2_id']}")
        assert _roi_do_bico(posto_2cam["bico_id"]) == ROI
        assert self._roi2(posto_2cam["bico_id"]) is None

    def test_camera_que_nao_e_do_bico_e_recusada(self, admin, posto_2cam):
        """Adivinhar o slot gravaria a área certa no lugar errado, sem erro na hora."""
        r = admin.put(f"/api/bicos/{posto_2cam['bico_id']}/roi",
                      json={**ROI, "camera_id": 9999})
        assert r.status_code == 400
        assert "não é deste bico" in r.json()["detail"]

    def test_trocar_a_segunda_camera_limpa_so_a_area_dela(self, admin, posto_2cam, outra_camera):
        admin.put(f"/api/bicos/{posto_2cam['bico_id']}/roi", json=ROI)
        admin.put(f"/api/bicos/{posto_2cam['bico_id']}/roi",
                  json={**ROI, "camera_id": posto_2cam["camera2_id"]})
        r = admin.put(f"/api/bicos/{posto_2cam['bico_id']}", json={
            "automacao_id": posto_2cam["automacao_id"], "codigo": "3",
            "camera_id": posto_2cam["camera_id"], "camera2_id": outra_camera})
        assert r.status_code == 200
        assert _roi_do_bico(posto_2cam["bico_id"]) == ROI      # slot 1 não foi tocado
        assert self._roi2(posto_2cam["bico_id"]) is None       # slot 2 invalidado

    def test_remover_a_segunda_camera_limpa_a_area_dela(self, admin, posto_2cam):
        admin.put(f"/api/bicos/{posto_2cam['bico_id']}/roi",
                  json={**ROI, "camera_id": posto_2cam["camera2_id"]})
        admin.put(f"/api/bicos/{posto_2cam['bico_id']}", json={
            "automacao_id": posto_2cam["automacao_id"], "codigo": "3",
            "camera_id": posto_2cam["camera_id"], "camera2_id": None})
        assert self._roi2(posto_2cam["bico_id"]) is None


class TestPreviewPorCamera:
    def test_camera_de_outro_bico_e_recusada(self, admin, posto_2cam):
        """Sem esta checagem o parâmetro viraria seletor de arquivo dentro de
        `dados_privados/` — a mesma classe de vazamento que tirou o preview de `static/`."""
        r = admin.get(f"/api/bicos/{posto_2cam['bico_id']}/preview.jpg?camera_id=9999")
        assert r.status_code == 404
        assert "não é deste bico" in r.json()["detail"]


class TestChecklistDoPosto:
    """`pronto`/`n_bicos_sem_roi` são o que a tela usa para dizer "falta configurar"."""

    def test_bico_sem_area_deixa_o_posto_incompleto(self, admin, posto):
        p = admin.get("/api/postos").json()[0]
        assert p["n_bicos_sem_roi"] == 1
        assert p["pronto"] is False

    def test_com_a_area_definida_o_posto_fica_pronto(self, admin, posto):
        admin.put(f"/api/bicos/{posto['bico_id']}/roi", json=ROI)
        p = admin.get("/api/postos").json()[0]
        assert p["n_bicos_sem_roi"] == 0
        assert p["pronto"] is True

    def test_bico_de_2_cameras_com_1_area_ainda_esta_incompleto(self, admin, posto_2cam):
        """Na câmera sem área a leitura analisa o QUADRO INTEIRO — que é justamente o que
        o recorte existe para evitar. Meio configurado não é configurado."""
        admin.put(f"/api/bicos/{posto_2cam['bico_id']}/roi", json=ROI)
        p = admin.get("/api/postos").json()[0]
        assert p["n_bicos_sem_roi"] == 1
        assert p["pronto"] is False

        admin.put(f"/api/bicos/{posto_2cam['bico_id']}/roi",
                  json={**ROI, "camera_id": posto_2cam["camera2_id"]})
        p = admin.get("/api/postos").json()[0]
        assert p["n_bicos_sem_roi"] == 0
        assert p["pronto"] is True
