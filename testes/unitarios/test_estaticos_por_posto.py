"""O guarda de `/static/snapshots/` (`_EstaticosPorPosto` em `app/servidor.py`).

Ele existe para fechar um vazamento LGPD real: sem checagem, um `cliente` do posto 4 pedia
`/static/snapshots/{ts}_{PLACA}.jpg` e recebia o carro do posto 7 — placa, imagem e
horário. Nem adivinhação era preciso, `/api/deteccoes` devolve a URL pronta.

O que estes testes protegem, e a tensão entre as duas metades:

1. **fechado para fora** — quem não é dono não vê, e recebe 404 (não 403), para não
   confirmar que o arquivo existe;
2. **aberto para quem tem direito** — e é aqui que mora a armadilha. A pasta
   `static/snapshots/` tem TRÊS escritores (leitura reativa, pipeline contínuo e
   `captura_dataset`), e só os dois primeiros criam linha em `deteccoes`. Resolver o dono
   exclusivamente pelo banco transforma toda imagem do terceiro em 404 — inclusive para
   admin, que não tem posto nenhum e deveria ver tudo.

O arquivo nasceu porque o guarda não tinha teste NENHUM: as duas metades passavam
despercebidas, e a segunda estava quebrada.
"""
from __future__ import annotations

import pytest

from app.core import banco

CONTEUDO = b"\xff\xd8\xff\xe0jpeg-falso"


@pytest.fixture
def snaps(ambiente, monkeypatch, tmp_path):
    """Redireciona o mount de estáticos para uma pasta temporária.

    O diretório real do repo tem 7000+ arquivos e é o insumo de outros testes — escrever
    nele acoplaria esta suíte ao estado do disco de quem roda.
    """
    from app import servidor

    pasta = tmp_path / "static" / "snapshots"
    pasta.mkdir(parents=True)
    # O StaticFiles resolve o caminho por `all_directories` (uma lista), não por
    # `directory` — trocar só o segundo deixaria o mount servindo a pasta real do repo e o
    # teste mediria o disco de quem roda. Os dois, e `monkeypatch` restaura ambos.
    raiz = str(tmp_path / "static")
    for rota in servidor.app.routes:
        if getattr(rota, "name", None) == "static":
            monkeypatch.setattr(rota.app, "directory", raiz)
            monkeypatch.setattr(rota.app, "all_directories", [raiz])
            break

    def _criar(nome: str) -> str:
        (pasta / nome).write_bytes(CONTEUDO)
        return f"/static/snapshots/{nome}"

    return _criar


class TestDonoResolvivel:
    """Imagem COM linha em `deteccoes`: o caminho que o guarda já cobria."""

    def test_admin_ve(self, snaps, admin, posto):
        url = snaps("20260101T000000_ABC1D23.jpg")
        banco.registrar_deteccao(placa="ABC1D23", padrao="mercosul", confianca=0.9,
                                 bico_id=posto["bico_id"], snapshot=url)
        r = admin.get(url)
        assert r.status_code == 200, r.text
        assert r.content == CONTEUDO


class TestSemDonoNoBanco:
    """Imagem SEM linha em `deteccoes` — as amostras do `captura_dataset`.

    É a fila de classificação de `/testes`: `app/web/testes.py` lista os arquivos direto do
    disco e monta `url = /static/snapshots/<nome>`. Nenhum deles tem detecção associada,
    porque a captura de dataset grava a imagem e nada mais.
    """

    def test_admin_ve_amostra_de_dataset(self, snaps, admin):
        """Admin não tem posto: negar a ele é negar a todo mundo.

        Enquanto o guarda resolvia o dono SÓ pelo banco, isto era 404 e a fila de
        classificação inteira aparecia com as imagens quebradas.
        """
        url = snaps("cap_20260101_120000_cam7.jpg")
        r = admin.get(url)
        assert r.status_code == 200, \
            "amostra de dataset não tem linha em `deteccoes` e mesmo assim é legítima"
        assert r.content == CONTEUDO

    def test_anonimo_nao_ve(self, snaps, admin):
        """A metade que NÃO pode afrouxar: sem login não se vê foto de veículo.

        Cliente NOVO e não a fixture `cliente`: aquela devolve o mesmo TestClient que
        `admin` autentica, então o "anônimo" sairia logado e o teste passaria por engano —
        exatamente o falso verde que este caso existe para impedir.

        `admin` na assinatura só para o sistema sair do bootstrap: sem nenhum usuário
        criado, `/criar-admin` é pública e o guarda de login não vale.
        """
        from starlette.testclient import TestClient
        from app.servidor import app

        url = snaps("cap_20260101_120000_cam7.jpg")
        r = TestClient(app).get(url, follow_redirects=False)
        assert r.status_code in (302, 303, 401, 403), \
            f"anônimo não pode ler snapshot (status {r.status_code})"

    def test_cliente_nao_ve_imagem_sem_dono(self, snaps, cliente_logado):
        """O preço de abrir o caso "sem dono": ele NÃO pode valer para quem tem escopo.

        Imagem sem posto não pertence ao posto do cliente, então continua 404 para ele.
        Sem esta asserção, a correção do 404 do dataset teria aberto toda foto órfã —
        justamente o vazamento que o guarda existe para fechar.
        """
        assert cliente_logado.get(snaps("cap_20260101_120000_cam7.jpg")).status_code == 404


class TestEscopoPorPosto:
    def test_cliente_de_outro_posto_leva_404(self, snaps, admin, posto, cliente_logado):
        """404 e não 403: não confirma a quem está fora do escopo que o arquivo existe.

        `cliente_logado` é do posto da fixture `posto`, então a detecção tem de nascer num
        posto DIFERENTE — senão o teste mediria o caso do dono e passaria por engano.
        """
        ent = admin.post("/api/entidades", json={"nome": "Rede B"}).json()["id"]
        emp = admin.post("/api/empresas", json={
            "entidade_id": ent, "nome": "Posto 2", "cnpj": "11444777000161"}).json()["id"]
        auto = admin.post("/api/automacoes",
                          json={"empresa_id": emp, "codigo": "9"}).json()["id"]
        cam = admin.post("/api/cameras", json={
            "nome": "Cam B", "empresa_id": emp, "camera_tipo": "rtsp",
            "rtsp_url_custom": "rtsp://x/9"}).json()["id"]
        bico = admin.post("/api/bicos", json={
            "automacao_id": auto, "codigo": "9", "camera_id": cam}).json()["id"]

        url = snaps("20260101T000000_XYZ9Z99.jpg")
        banco.registrar_deteccao(placa="XYZ9Z99", padrao="mercosul", confianca=0.9,
                                 bico_id=bico, snapshot=url)
        assert cliente_logado.get(url).status_code == 404
        assert admin.get(url).status_code == 200, "o dado existe; é o escopo que barra"
