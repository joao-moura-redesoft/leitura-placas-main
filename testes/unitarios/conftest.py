"""Fixtures da suíte automatizada.

Isolamento: cada teste que toca banco/config recebe um SQLite e um config.txt próprios
num diretório temporário. `banco.definir_caminho` já solta a conexão thread-local junto
— sem isso a thread continuaria falando com o banco do teste anterior.

O TestClient é criado SEM `with`: o lifespan da aplicação sobe pipeline de câmera e
pré-carrega os modelos de detecção/OCR (dezenas de segundos), coisas que nenhum teste
aqui exercita. `banco.inicializar()` é chamado à mão para compensar a única parte do
startup de que os testes dependem.
"""
from __future__ import annotations
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import banco, config
from app.seguranca import limitador
from app.seguranca import sessao as auth_mod
from app.seguranca import tentativas


@pytest.fixture(autouse=True)
def _bcrypt_barato(monkeypatch):
    """Custo mínimo do bcrypt na suíte.

    Com o custo de produção (12), cada usuário criado numa fixture custa ~0,2s e a
    suíte passa a maior parte do tempo derivando hash. O que os testes verificam é a
    lógica em volta da senha — que ela é hasheada, comparada e trocada nas horas certas
    — e isso não depende do número de rodadas.
    """
    monkeypatch.setattr(auth_mod, "_BCRYPT_ROUNDS", 4)


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    """Banco e configuração limpos, isolados por teste."""
    banco.definir_caminho(tmp_path / "teste.db")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.txt")
    tentativas._resetar_para_teste()
    limitador._resetar_para_teste()
    banco.inicializar()
    yield tmp_path
    banco.fechar_conexao()


@pytest.fixture
def cliente(ambiente):
    """Cliente HTTP sem ninguém logado."""
    from app.servidor import app
    return TestClient(app)


def _autenticar(cli: TestClient, resposta) -> TestClient:
    """Deixa exatamente UM cookie de sessão no cliente.

    O TestClient já guarda o Set-Cookie da resposta; regravar por cima cria uma segunda
    entrada no jar (uma por domínio) e qualquer leitura de `cli.cookies["sessao"]`
    depois disso estoura com CookieConflict.
    """
    token = resposta.cookies["sessao"]
    cli.cookies.clear()
    cli.cookies.set("sessao", token)
    return cli


def _criar_admin(cliente: TestClient, email: str = "admin@teste.com") -> TestClient:
    r = cliente.post("/criar-admin", follow_redirects=False, data={
        "nome": "Admin", "email": email, "senha": "senha-admin-1", "confirmar": "senha-admin-1",
    })
    assert r.status_code == 303, "bootstrap do admin falhou"
    return _autenticar(cliente, r)


@pytest.fixture
def admin(cliente):
    """Cliente logado como administrador (é também o primeiro usuário do sistema)."""
    return _criar_admin(cliente)


@pytest.fixture
def posto(admin):
    """Cadastro mínimo completo: entidade → empresa → automação → câmera → bico.

    Devolve os ids. Vários testes precisam de um cadastro resolvível e montá-lo à mão
    em cada um esconderia o que o teste realmente exercita.
    """
    ent = admin.post("/api/entidades", json={"nome": "Rede Teste"}).json()["id"]
    emp = admin.post("/api/empresas", json={
        "entidade_id": ent, "nome": "Posto 1", "cnpj": "11222333000181",
    })
    assert emp.status_code == 200, emp.text
    emp = emp.json()["id"]
    auto = admin.post("/api/automacoes", json={"empresa_id": emp, "codigo": "1"}).json()["id"]
    cam = admin.post("/api/cameras", json={
        "nome": "Cam 1", "empresa_id": emp, "camera_tipo": "rtsp", "rtsp_url_custom": "rtsp://x/1",
    }).json()["id"]
    bico = admin.post("/api/bicos", json={
        "automacao_id": auto, "codigo": "3", "camera_id": cam,
    })
    assert bico.status_code == 200, bico.text
    return {"entidade_id": ent, "empresa_id": emp, "automacao_id": auto,
            "camera_id": cam, "bico_id": bico.json()["id"], "cnpj": "11222333000181"}


@pytest.fixture
def cliente_logado(admin, posto):
    """Cliente HTTP logado como usuário 'cliente' — restrito ao posto da fixture
    `posto`, em sessão separada da do admin."""
    from app.servidor import app
    r = admin.post("/api/usuarios", json={
        "nome": "Cliente", "email": "cliente@teste.com",
        "senha": "senha-cliente-1", "papel": "cliente", "empresa_id": posto["empresa_id"],
    })
    assert r.status_code == 200, r.text

    cli = TestClient(app)
    r = cli.post("/login", follow_redirects=False,
                 data={"email": "cliente@teste.com", "senha": "senha-cliente-1"})
    assert r.status_code == 303, "login do cliente falhou"
    return _autenticar(cli, r)


@pytest.fixture
def operador_logado(admin):
    """Cliente HTTP logado como usuário 'operador' — vê todos os postos (não é preso
    a nenhum), mas não passa em `deps.exigir_admin`."""
    from app.servidor import app
    r = admin.post("/api/usuarios", json={
        "nome": "Operador", "email": "operador@teste.com",
        "senha": "senha-operador-1", "papel": "operador",
    })
    assert r.status_code == 200, r.text

    cli = TestClient(app)
    r = cli.post("/login", follow_redirects=False,
                 data={"email": "operador@teste.com", "senha": "senha-operador-1"})
    assert r.status_code == 303, "login do operador falhou"
    return _autenticar(cli, r)


@pytest.fixture(autouse=True)
def _sem_visao(monkeypatch):
    """Mantém a suíte fora de câmera e de modelo de visão.

    Duas coisas puxam isso sem que o teste peça. Cadastrar uma câmera faz a rota subir
    o pipeline dela numa thread — que abre RTSP e carrega detector + OCR (dezenas de
    segundos e centenas de MB) para uma câmera que não existe. E chamar uma rota de
    leitura por engano carregaria os mesmos modelos.

    Pipeline vira no-op; os carregadores de modelo passam a falhar alto, para um teste
    que dependa deles denunciar isso na hora em vez de ficar lento em silêncio. Acurácia
    de visão é medida pelo harness em `testes/`, não aqui.
    """
    def _explodir(*_a, **_kw):
        raise AssertionError(
            "teste tentou carregar modelo de visão — a suíte unitária não deve chegar aqui")

    import app.visao.detector as det
    import app.visao.ocr as ocr
    import app.visao.pipeline as pipe
    import app.web.api as api_mod

    monkeypatch.setattr(det, "obter_detector_leitura", _explodir)
    monkeypatch.setattr(ocr, "obter_ocr_leitura", _explodir)
    for nome in ("iniciar_camera", "reiniciar_camera", "parar_camera", "iniciar_cameras_db"):
        monkeypatch.setattr(pipe, nome, lambda *_a, **_kw: None)
    monkeypatch.setattr(api_mod, "_iniciar_camera_bg", lambda *_a, **_kw: None)
