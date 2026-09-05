"""`/static/snapshots/` é escopado por posto.

O mount de `/static` exigia LOGIN mas não checava DONO, e `snapshots/` guarda foto de
veículo. Um `cliente` do posto A pedia `/static/snapshots/{ts}_{PLACA}.jpg` e recebia o
carro do posto B — placa, imagem e horário, que é dado pessoal (LGPD). E não precisava
adivinhar o nome: `/api/deteccoes` devolve a URL pronta no campo `snapshot`.

É a mesma falha que o HLS já tinha corrigido com `_HlsPorPosto` (auditoria 27/08, achado
A4); o histórico de leitura tinha ficado de fora. Estes testes prendem as duas metades: o
alheio não passa, e o próprio continua passando — porque "fechar" negando tudo quebraria
a tela de histórico do cliente, que é o uso legítimo.
"""
from __future__ import annotations

import pytest

from app.core import banco


def _gravar_foto(empresa_id: int, camera_id: int, placa: str) -> str:
    """Cria a foto no disco e a linha de `deteccoes` que a referencia.

    Grava por baixo do HTTP de propósito: o caminho de leitura de verdade (`ler_placa`)
    está dublado na suíte unitária (`_sem_visao`), e o que se testa aqui é o SERVIÇO do
    arquivo, não como ele nasceu.
    """
    from app.visao import leitura as leitura_mod

    url_rel = f"/static/snapshots/20260101T000000_{placa}.jpg"
    leitura_mod.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    (leitura_mod.SNAPSHOT_DIR / f"20260101T000000_{placa}.jpg").write_bytes(b"\xff\xd8\xff-jpeg-falso")
    banco.registrar_deteccao(placa=placa, padrao="mercosul", confianca=0.9,
                             snapshot=url_rel, camera_db_id=camera_id)
    return url_rel


@pytest.fixture
def dois_postos(admin, posto):
    """O posto da fixture `posto` (do `cliente_logado`) e um SEGUNDO posto alheio a ele."""
    ent2 = admin.post("/api/entidades", json={"nome": "Rede Y"}).json()["id"]
    emp2 = admin.post("/api/empresas", json={
        "entidade_id": ent2, "nome": "Posto 2", "cnpj": "11444777000161",
    }).json()["id"]
    cam2 = admin.post("/api/cameras", json={
        "nome": "Cam do posto 2", "empresa_id": emp2,
        "camera_tipo": "rtsp", "rtsp_url_custom": "rtsp://x/9",
    }).json()["id"]
    return {"empresa_id": emp2, "camera_id": cam2}


class TestClienteNaoVeFotoDeOutroPosto:

    def test_foto_do_outro_posto_nao_sai(self, cliente_logado, posto, dois_postos):
        """O vazamento em si: cliente do posto 1 pedindo a foto do posto 2."""
        url = _gravar_foto(dois_postos["empresa_id"], dois_postos["camera_id"], "XXX9Z99")
        r = cliente_logado.get(url)
        assert r.status_code == 404, f"vazou foto de outro posto: {r.status_code}"
        assert b"jpeg-falso" not in r.content

    def test_404_e_nao_403(self, cliente_logado, dois_postos):
        """404 e não 403 pelo mesmo motivo do HLS: um 403 confirmaria a quem está fora do
        escopo que aquele arquivo existe, que é o oráculo que `checar_acesso_empresa`
        existe para fechar."""
        url = _gravar_foto(dois_postos["empresa_id"], dois_postos["camera_id"], "YYY8W88")
        assert cliente_logado.get(url).status_code == 404

    def test_arquivo_orfao_nao_sai(self, cliente_logado):
        """Foto no disco que nenhuma detecção referencia (retenção já apagou a linha).

        Sem dono resolvível não há como afirmar que o pedinte pode ver — e o aberto por
        omissão seria o vazamento de volta.
        """
        from app.visao import leitura as leitura_mod
        leitura_mod.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        (leitura_mod.SNAPSHOT_DIR / "20260101T000000_ORF0A00.jpg").write_bytes(b"\xff\xd8orfao")
        r = cliente_logado.get("/static/snapshots/20260101T000000_ORF0A00.jpg")
        assert r.status_code == 404
        assert b"orfao" not in r.content


class TestOQueTemDePassarContinuaPassando:
    """A metade que evita "consertar" negando tudo."""

    def test_cliente_ve_a_foto_do_proprio_posto(self, cliente_logado, posto):
        url = _gravar_foto(posto["empresa_id"], posto["camera_id"], "ABC1D23")
        r = cliente_logado.get(url)
        assert r.status_code == 200, f"cliente perdeu acesso à PRÓPRIA foto: {r.text[:200]}"
        assert b"jpeg-falso" in r.content

    def test_admin_ve_qualquer_foto(self, admin, dois_postos):
        url = _gravar_foto(dois_postos["empresa_id"], dois_postos["camera_id"], "ZZZ7K77")
        assert admin.get(url).status_code == 200

    def test_css_e_js_nao_passam_pela_checagem(self, cliente_logado):
        """Só `snapshots/` é escopado. Estáticos comuns são o volume do site e não são
        dado de cliente — mandá-los ao banco a cada request seria um SELECT por arquivo."""
        r = cliente_logado.get("/static/css/base.css")
        assert r.status_code == 200
        assert b"{" in r.content
