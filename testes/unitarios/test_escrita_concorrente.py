"""Corridas entre dois clientes simultâneos (auditoria de 05/09/2026).

Todos os casos aqui nasceram de bug REPRODUZIDO, e o cenário é sempre o mesmo: duas
máquinas usando o painel ao mesmo tempo. O que eles fixam:

  * `config.txt` publicado atomicamente — o leitor (middleware de autenticação!) nunca vê
    o arquivo pela metade. Media 185 leituras quebradas em 1.155, e cada uma recusava com
    401 uma chamada VÁLIDA de /api/leitura de um posto.
  * `config.alterar()` serializa carregar→modificar→salvar — duas edições não se apagam.
  * preview de bico publicado atomicamente — media 17.400 JPEGs truncados contra 253 bons.
  * a trava do último admin aplicada DENTRO do UPDATE — dois admins rebaixando um ao outro
    deixavam o sistema com ZERO admin, trancando todo mundo fora do painel.

Ao contrário do resto da suíte, estes testes usam threads de verdade: a corrida é o objeto
do teste, e um dublê que serializasse as duas pontas mediria justamente o que não importa.
"""
from __future__ import annotations

import threading

import numpy as np
import pytest

from app.core import arquivos, banco, config


# ── config.txt ───────────────────────────────────────────────────────────────

def test_leitor_nunca_ve_config_truncado(tmp_path, monkeypatch):
    """O sintoma que motivou tudo: `api_key` sumindo do dict durante um save."""
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.txt")
    config.salvar({**config.PADROES, "api_key": "CHAVE-DO-SERVIDOR"})

    quebrados: list[str] = []
    parar = threading.Event()

    def leitor():   # o middleware de auth resolvendo X-API-Key (app/servidor.py)
        while not parar.is_set():
            try:
                if config.carregar().get("api_key") != "CHAVE-DO-SERVIDOR":
                    quebrados.append("api_key ausente")
            except Exception as e:      # noqa: BLE001 — qualquer erro aqui é falha
                quebrados.append(f"{type(e).__name__}: {e}")

    r = threading.Thread(target=leitor)
    r.start()
    try:
        for _ in range(120):
            with config.alterar() as cfg:
                cfg["log_level"] = "info"
    finally:
        parar.set()
        r.join(timeout=10)

    assert quebrados == [], f"{len(quebrados)} leitura(s) ruim(ns): {quebrados[:3]}"


def test_duas_edicoes_de_config_nao_se_apagam(tmp_path, monkeypatch):
    """Dois PCs salvam campos DIFERENTES quase juntos — os dois têm de sobreviver.

    Antes do `alterar()`, o segundo gravava por cima do snapshot que carregou ANTES do
    primeiro salvar: a tela dele mostrava sucesso e o valor do outro tinha desaparecido.
    """
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.txt")
    config.salvar({**config.PADROES, "intelbras_senha": "", "api_key": ""})

    porta = threading.Barrier(2)

    def salva(chave: str, valor: str):
        porta.wait(timeout=5)       # maximiza a sobreposição
        with config.alterar() as cfg:
            cfg[chave] = valor

    fios = [threading.Thread(target=salva, args=a) for a in
            (("intelbras_senha", "SENHA-DA-CAMERA"), ("api_key", "CHAVE-DO-POSTO"))]
    for f in fios:
        f.start()
    for f in fios:
        f.join(timeout=10)

    final = config.carregar()
    assert final["intelbras_senha"] == "SENHA-DA-CAMERA"
    assert final["api_key"] == "CHAVE-DO-POSTO"


def test_alterar_nao_grava_se_o_bloco_levanta(tmp_path, monkeypatch):
    """Validação que falha no meio do bloco não deixa config pela metade no disco."""
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.txt")
    config.salvar({**config.PADROES, "log_level": "info"})

    with pytest.raises(ValueError):
        with config.alterar() as cfg:
            cfg["log_level"] = "debug"
            raise ValueError("payload inválido")

    assert config.carregar()["log_level"] == "info"


# ── escrita atômica genérica ─────────────────────────────────────────────────

def test_texto_atomico_nao_deixa_temporario_para_tras(tmp_path):
    alvo = tmp_path / "x.txt"
    for i in range(20):
        arquivos.escrever_texto_atomico(alvo, f"v{i}")
    assert alvo.read_text(encoding="utf-8") == "v19"
    # Nome de temporário é único por escritor; se sobrasse algum, ninguém os apagaria.
    assert list(tmp_path.glob("*.tmp")) == []


def test_temporario_e_removido_quando_a_escrita_falha(tmp_path, monkeypatch):
    alvo = tmp_path / "x.txt"
    monkeypatch.setattr(arquivos, "_replace_com_retry",
                        lambda *a: (_ for _ in ()).throw(OSError("disco cheio")))
    with pytest.raises(OSError):
        arquivos.escrever_texto_atomico(alvo, "conteudo")
    assert list(tmp_path.glob("*.tmp")) == []


def test_preview_nunca_e_servido_truncado(tmp_path):
    """Dois PCs na tela de captura do mesmo bico: um lê o preview, o outro o reescreve."""
    alvo = tmp_path / "preview_bico_1.jpg"
    a = np.random.randint(0, 255, (240, 320, 3), dtype=np.uint8)
    b = np.random.randint(0, 255, (240, 320, 3), dtype=np.uint8)
    arquivos.imwrite_atomico(alvo, a, 80)

    ruins: list[str] = []
    parar = threading.Event()

    def leitor():       # o que a rota /api/bicos/{id}/preview.jpg entrega ao navegador
        while not parar.is_set():
            try:
                dados = arquivos.ler_bytes_com_retry(alvo)
            except Exception as e:      # noqa: BLE001
                ruins.append(f"{type(e).__name__}: {e}")
                continue
            # JPEG íntegro termina em FFD9 (End Of Image).
            if not dados.endswith(b"\xff\xd9"):
                ruins.append(f"JPEG truncado ({len(dados)} bytes)")

    r = threading.Thread(target=leitor)
    r.start()
    try:
        for i in range(60):
            arquivos.imwrite_atomico(alvo, a if i % 2 else b, 80, tolerar_falha=True)
    finally:
        parar.set()
        r.join(timeout=10)

    assert ruins == [], f"{len(ruins)} leitura(s) ruim(ns): {ruins[:3]}"


def test_preview_tolerante_mantem_o_arquivo_anterior(tmp_path, monkeypatch):
    """Falhar em atualizar preview NÃO pode derrubar a leitura de placa do posto."""
    alvo = tmp_path / "preview_bico_1.jpg"
    img = np.zeros((32, 32, 3), dtype=np.uint8)
    assert arquivos.imwrite_atomico(alvo, img, 80)
    antes = alvo.read_bytes()

    monkeypatch.setattr(arquivos, "_replace_com_retry",
                        lambda *a: (_ for _ in ()).throw(OSError("ocupado")))
    assert arquivos.imwrite_atomico(alvo, img, 80, tolerar_falha=True) is False
    assert alvo.read_bytes() == antes, "preview velho tinha de ser preservado"

    # Sem `tolerar_falha`, propaga: snapshot de histórico é referenciado por linha do
    # banco, e falhar em silêncio deixaria a linha apontando para arquivo inexistente.
    with pytest.raises(OSError):
        arquivos.imwrite_atomico(alvo, img, 80)


# ── último administrador ─────────────────────────────────────────────────────

def _dados(nome: str, email: str) -> dict:
    return {"nome": nome, "email": email, "papel": "cliente",
            "ativo": 1, "empresa_id": None}


class TestUltimoAdmin:
    """A trava tem de valer com DOIS admins agindo ao mesmo tempo, não só em série."""

    def test_dois_admins_se_rebaixando_juntos_deixam_um_de_pe(self, cliente):
        """Reproduzia ZERO admin ativo: os dois passavam pela checagem pré-UPDATE."""
        a = banco.criar_usuario("A", "a@x.com", "h", papel="admin")
        b = banco.criar_usuario("B", "b@x.com", "h", papel="admin")

        from app.core.banco import _base
        porta = threading.Barrier(2)
        veredito: list[str] = []

        def rebaixa(quem: int, email: str):
            _base.fechar_conexao()      # conexão sqlite é por thread
            porta.wait(timeout=5)
            try:
                banco.usuarios_atualizar(quem, _dados("X", email),
                                         exigir_outro_admin=True)
                veredito.append("rebaixado")
            except banco.UltimoAdminError:
                veredito.append("barrado")

        fios = [threading.Thread(target=rebaixa, args=(q, e))
                for q, e in ((b, "b@x.com"), (a, "a@x.com"))]
        for f in fios:
            f.start()
        for f in fios:
            f.join(timeout=10)

        assert banco.usuarios_contar_admins_ativos() >= 1, \
            "sistema ficou sem administrador — painel inacessível"
        assert sorted(veredito) == ["barrado", "rebaixado"], veredito

    def test_a_trava_continua_barrando_o_unico_admin(self, cliente):
        """A correção não pode ter afrouxado o caso simples."""
        so_ele = banco.criar_usuario("Único", "u@x.com", "h", papel="admin")
        with pytest.raises(banco.UltimoAdminError):
            banco.usuarios_atualizar(so_ele, _dados("Único", "u@x.com"),
                                     exigir_outro_admin=True)
        assert banco.usuarios_contar_admins_ativos() == 1

    def test_rebaixar_com_outro_admin_de_pe_funciona(self, cliente):
        banco.criar_usuario("Fica", "fica@x.com", "h", papel="admin")
        sai = banco.criar_usuario("Sai", "sai@x.com", "h", papel="admin")
        assert banco.usuarios_atualizar(sai, _dados("Sai", "sai@x.com"),
                                        exigir_outro_admin=True) is True
        assert banco.buscar_usuario_id(sai)["papel"] == "cliente"

    def test_id_inexistente_nao_vira_ultimo_admin(self, cliente):
        """`False` (não achei a linha) e `UltimoAdminError` (a guarda barrou) são coisas
        diferentes — confundi-las faria a rota responder 400 no lugar de 404."""
        banco.criar_usuario("Admin", "adm@x.com", "h", papel="admin")
        assert banco.usuarios_atualizar(99999, _dados("Fantasma", "f@x.com"),
                                        exigir_outro_admin=True) is False
