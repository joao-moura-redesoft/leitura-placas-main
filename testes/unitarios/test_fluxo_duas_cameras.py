"""Fluxo completo de configurar um bico com duas câmeras, pela API de verdade.

Os testes de `test_cadastro.py`/`test_roi_bico.py` cobrem cada peça isolada; aqui o que
se verifica é a JUNÇÃO — que o dado que o editor de áreas precisa chega no formato certo,
que as duas áreas sobrevivem à ida e volta, e que as páginas envolvidas ainda renderizam.
Sem isto, uma peça pode estar certa e o operador ainda assim não conseguir configurar o
bico, que é o único desfecho que importa.
"""
from __future__ import annotations

import json

ROI_TRAS = {"x": 10, "y": 20, "w": 300, "h": 200}
ROI_FRENTE = {"x": 40, "y": 50, "w": 260, "h": 180}


class TestConfiguracaoPelaApi:
    def test_o_bico_aparece_no_editor_das_duas_cameras(self, admin, posto_2cam):
        """O editor é POR CÂMERA e lista os bicos dela. Um bico de duas tem que aparecer
        nos dois — senão não há como desenhar a segunda área."""
        for cam_id in (posto_2cam["camera_id"], posto_2cam["camera2_id"]):
            d = admin.get(f"/api/cameras/{cam_id}/detalhe").json()
            assert [b["id"] for b in d["bicos"]] == [posto_2cam["bico_id"]]

    def test_cada_editor_recebe_o_slot_e_a_outra_camera(self, admin, posto_2cam):
        """Quem decide o slot é o servidor: deixar o cliente escolher gravaria a área na
        câmera errada, sem erro nenhum na hora."""
        d1 = admin.get(f"/api/cameras/{posto_2cam['camera_id']}/detalhe").json()["bicos"][0]
        d2 = admin.get(f"/api/cameras/{posto_2cam['camera2_id']}/detalhe").json()["bicos"][0]

        assert (d1["slot"], d1["papel_nesta_camera"]) == (1, "traseira")
        assert (d2["slot"], d2["papel_nesta_camera"]) == (2, "frente")
        # Cada editor sabe apontar para o outro — é o que impede o operador de desenhar
        # uma área e ir embora achando que terminou.
        assert d1["outra_camera_id"] == posto_2cam["camera2_id"]
        assert d2["outra_camera_id"] == posto_2cam["camera_id"]

    def test_as_duas_areas_sobrevivem_a_ida_e_volta(self, admin, posto_2cam):
        cam1, cam2 = posto_2cam["camera_id"], posto_2cam["camera2_id"]
        admin.put(f"/api/bicos/{posto_2cam['bico_id']}/roi", json={**ROI_TRAS, "camera_id": cam1})
        admin.put(f"/api/bicos/{posto_2cam['bico_id']}/roi", json={**ROI_FRENTE, "camera_id": cam2})

        def roi_no_editor(cam_id):
            b = admin.get(f"/api/cameras/{cam_id}/detalhe").json()["bicos"][0]
            bruto = b["roi_nesta_camera"]
            return json.loads(bruto) if isinstance(bruto, str) else bruto

        assert roi_no_editor(cam1) == ROI_TRAS
        assert roi_no_editor(cam2) == ROI_FRENTE

    def test_o_detalhe_do_posto_descreve_as_duas_fontes(self, admin, posto_2cam):
        """É o que a tela do posto usa para montar os botões e o checklist."""
        admin.put(f"/api/bicos/{posto_2cam['bico_id']}/roi",
                  json={**ROI_TRAS, "camera_id": posto_2cam["camera_id"]})
        bico = admin.get(f"/api/postos/{posto_2cam['empresa_id']}").json()["automacoes"][0]["bicos"][0]

        assert [f["papel"] for f in bico["cameras"]] == ["traseira", "frente"]
        assert [f["tem_roi"] for f in bico["cameras"]] == [True, False]
        assert bico["rois_faltando"] == [posto_2cam["camera2_id"]]

    def test_voltar_para_uma_camera_so(self, admin, posto_2cam):
        """A feature é opcional nos dois sentidos — dá para desfazer."""
        r = admin.put(f"/api/bicos/{posto_2cam['bico_id']}", json={
            "automacao_id": posto_2cam["automacao_id"], "codigo": "3",
            "camera_id": posto_2cam["camera_id"], "camera2_id": None})
        assert r.status_code == 200
        bico = admin.get(f"/api/postos/{posto_2cam['empresa_id']}").json()["automacoes"][0]["bicos"][0]
        assert len(bico["cameras"]) == 1
        # E a câmera liberada pode ser removida, agora que ninguém a usa
        assert admin.delete(f"/api/cameras/{posto_2cam['camera2_id']}").status_code == 200


class TestPaginasRenderizam:
    """Erro de template só aparece quando alguém abre a página — que é tarde demais."""

    def test_tela_do_posto(self, admin, posto_2cam):
        r = admin.get(f"/posto/{posto_2cam['empresa_id']}")
        assert r.status_code == 200
        assert "Testar como o roteador" in r.text

    def test_editor_de_areas_das_duas_cameras(self, admin, posto_2cam):
        for cam_id in (posto_2cam["camera_id"], posto_2cam["camera2_id"]):
            assert admin.get(f"/roi-camera/{cam_id}").status_code == 200

    def test_atalho_por_bico_leva_ao_editor(self, admin, posto_2cam):
        r = admin.get(f"/roi-bico/{posto_2cam['bico_id']}", follow_redirects=False)
        assert r.status_code == 303
        # Vai para a PRIMEIRA câmera; de lá o link "também usa…" leva à segunda.
        assert f"/roi-camera/{posto_2cam['camera_id']}" in r.headers["location"]

    def test_atalho_por_bico_nao_resolve_para_cliente(self, cliente_logado, posto_2cam):
        """O destino (/roi-camera) e admin-only, mas o atalho resolvia bico_id -> camera_id
        no `Location` para qualquer usuario logado. bico_id e um inteiro sequencial: bastava
        iterar para mapear a relacao bico/camera de todos os postos. Vale mesmo sendo o
        posto DELE — o gate aqui e de papel, igual ao do editor.
        """
        r = cliente_logado.get(f"/roi-bico/{posto_2cam['bico_id']}", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/postos"
        assert "roi-camera" not in r.headers["location"]
