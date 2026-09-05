"""MODO LIVRE da vitrine: a placa NÃO cadastrada também aparece na tela (`feira_livre`).

O caso de uso é o visitante do estande apontando para o próprio carro em vez do carrinho
de demonstração. Três coisas se somam aqui, e cada uma tem seu jeito de dar errado:

1. **exibição** — `feira.livre` afrouxa a trava que só deixava passar placa mockada;
2. **custo** — a placa livre pode custar uma consulta paga, e SÓ pelo botão "Forçar
   leitura" (`forcar=1`). O loop hands-free varre a cada ~1,6s sem ninguém pedir, e a
   câmera do estande enxerga o celular na mão de quem passa: ali cada leitura acidental
   viraria crédito queimado e dado de veículo de terceiro numa TV;
3. **offline** — sem internet/token a placa aparece sozinha, sem card afirmando dados
   que não existem. É o estado real da máquina de feira.

Como em `test_leitura_veiculo.py`, os casos de custo afirmam sobre um contador de chamadas
à função de FRONTEIRA (`buscar_na_api`) — a evidência direta de que não se gastou — em vez
de reexecutar a regra que decide gastar.
"""
from __future__ import annotations

import pytest

from app.core import config
from app.integracoes import apiplacas
from app.seguranca import limitador
from app.visao import feira
from app.web import cadastro as cadastro_rotas

# Recorte da resposta 200 real da apiplacas, como nos outros arquivos do módulo.
DOC = {
    "marca": "VW", "modelo": "CROSSFOX", "ano": "2007", "anoModelo": "2007",
    "cor": "Prata", "municipio": "São Leopoldo", "uf": "RS", "situacao": "Sem restrição",
    "extra": {"combustivel": "Alcool / Gasolina", "especie": "Passageiro",
              "tipo_veiculo": "Automovel", "cilindradas": "1599"},
    "fipe": {"dados": [{"score": 101, "sigla_combustivel": "G",
                        "combustivel": "Gasolina"}]},
}

VISITANTE = "ABC1D23"        # placa livre: não está em `feira_placas` nem tem ficha
DEMO = "MOK3H92"             # o carrinho canônico


def _resultado(**over) -> dict:
    """Retorno de `ler_placa` para uma leitura BOA — a que `_pode_gastar` aprova."""
    base = {
        "camera_id": 7, "bico_id": 1, "placa": VISITANTE, "padrao": "mercosul",
        "confianca": 0.95, "votos_snapshot": 2, "total_snapshots": 2, "votos_ocr": 3,
        "total_engines": 3, "detalhes_ocr": [], "snapshot": None, "frame_url": None,
        "tentativas": 2, "acordo": 1.0, "confirmada": True, "parada_motivo": "acordo",
        "tipo_veiculo": "carro", "n_cameras_votando": 1, "fontes": [], "avisos": [],
        "mockada": False,
    }
    base.update(over)
    return base


@pytest.fixture
def vitrine(ambiente, admin, posto, monkeypatch):
    """Posto de demonstração armado, modo livre LIGADO e a fronteira HTTP contada."""
    config.salvar({**config.carregar(),
                   "feira_ativo": "sim", "feira_placas": DEMO,
                   "feira_empresa_id": str(posto["empresa_id"]),
                   "feira_livre": "sim", "feira_livre_consulta": "sim",
                   # `automatico` explícito: em `manual` NADA consulta sozinho, e todos os
                   # casos de gasto passariam pelo motivo errado (falso verde).
                   "apiplacas_ativo": "sim", "apiplacas_modo": "automatico",
                   "apiplacas_token": "TOKEN-SECRETO"})
    limitador._resetar_para_teste()
    apiplacas.limpar_pausa()

    estado = {"resultado": _resultado()}
    chamadas: list[str] = []

    monkeypatch.setattr(cadastro_rotas.leitura, "ler_placa",
                        lambda **kw: dict(estado["resultado"]))

    def _fronteira(placa, token, timeout_seg, base_url):
        chamadas.append(placa)
        return (200, DOC, "")

    monkeypatch.setattr(apiplacas, "buscar_na_api", _fronteira)

    def _scan(forcar: bool = False, **over):
        if over:
            estado["resultado"] = _resultado(**over)
        # `limitador`: a rota tem teto de 6/min por IP, e uma classe com vários scans
        # estouraria o limite e receberia 429 em vez do que está sendo medido.
        limitador._resetar_para_teste()
        return admin.post("/api/feira/scan", params={"forcar": 1} if forcar else {})

    yield type("V", (), {"scan": staticmethod(_scan), "chamadas": chamadas})()
    apiplacas.limpar_pausa()


class TestExibicao:
    def test_placa_livre_e_revelada(self, vitrine):
        """O requisito inteiro em um teste: a placa do visitante chega à tela."""
        d = vitrine.scan().json()
        assert d["placa"] == VISITANTE
        assert d["livre"] is True, "é o sinal que o kiosk usa para revelar o card"
        assert d["mockada"] is False, "não é o carrinho, e o payload não pode dizer que é"

    def test_desligado_nao_revela(self, vitrine):
        """Default do sistema: sem `feira_livre` a vitrine segue só com o carrinho."""
        config.salvar({**config.carregar(), "feira_livre": "nao"})
        d = vitrine.scan().json()
        assert d["livre"] is False
        assert d["veiculo"] is None, "sem exibição não há bloco de veículo"

    def test_livre_sozinho_arma_a_rota(self, vitrine):
        """`feira.ativo` exige lista de placas; o modo livre é justamente o estande que
        não cadastrou carrinho nenhum. Sem o `ou livre` na rota isto viraria 409."""
        config.salvar({**config.carregar(), "feira_placas": ""})
        assert feira.ativo(config.carregar()) is False
        r = vitrine.scan()
        assert r.status_code == 200, r.text
        assert r.json()["placa"] == VISITANTE

    def test_sem_posto_de_demonstracao_fica_desarmado(self, vitrine):
        """Fail-closed: ligar o interruptor não pode, sozinho, transformar posto real em
        telão. É a mesma trava que `feira.ativo` já fazia."""
        config.salvar({**config.carregar(), "feira_empresa_id": ""})
        assert feira.livre(config.carregar()) is False
        assert vitrine.scan().status_code == 409


class TestCusto:
    def test_forcar_consulta_a_placa_livre(self, vitrine):
        """O botão "Forçar leitura": humano apertando, cliente ao lado. É onde o gasto vale."""
        v = vitrine.scan(forcar=True).json()["veiculo"]
        assert vitrine.chamadas == [VISITANTE]
        assert v["consulta"] == "ok"
        assert v["combustivel"] == "Alcool / Gasolina"
        assert v["modelo"] == "CROSSFOX"

    def test_loop_automatico_nao_gasta(self, vitrine):
        """A trava que mais importa deste arquivo.

        O loop varre sozinho a cada ~1,6s. Se ele consultasse, um estande aberto o dia
        inteiro queimaria a cota em minutos — e cada celular de visitante lido por acaso
        viraria dado de um veículo real de terceiro exibido na TV.
        """
        d = vitrine.scan(forcar=False).json()
        assert vitrine.chamadas == [], "loop hands-free NUNCA gasta"
        assert d["placa"] == VISITANTE, "mas a placa continua sendo revelada"
        assert d["veiculo"] is None

    def test_consulta_desligada_so_mostra_a_placa(self, vitrine):
        """`feira_livre_consulta=nao`: exibe sem nunca gastar, nem no botão."""
        config.salvar({**config.carregar(), "feira_livre_consulta": "nao"})
        d = vitrine.scan(forcar=True).json()
        assert vitrine.chamadas == []
        assert d["placa"] == VISITANTE and d["veiculo"] is None

    def test_leitura_ruim_nao_gasta_nem_forcada(self, vitrine):
        """`_pode_gastar` é reusada, não reescrita: pagar por leitura não confirmada é
        gasto certo por benefício nenhum, aqui tanto quanto no roteador."""
        d = vitrine.scan(forcar=True, confirmada=False, acordo=0.4).json()
        assert vitrine.chamadas == []
        assert d["placa"] == VISITANTE

    def test_modo_manual_respeitado(self, vitrine):
        """Em `manual` nada consulta sozinho — a vitrine não é exceção."""
        config.salvar({**config.carregar(), "apiplacas_modo": "manual"})
        vitrine.scan(forcar=True)
        assert vitrine.chamadas == []


class TestOffline:
    """O cenário REAL do estande: sem rede e sem token."""

    def test_sem_internet_mostra_so_a_placa(self, vitrine):
        """O pedido literal: "caso não [tenha internet], só mostra a placa"."""
        config.salvar({**config.carregar(), "apiplacas_ativo": "nao"})
        d = vitrine.scan(forcar=True).json()
        assert d["placa"] == VISITANTE
        assert d["livre"] is True, "a revelação não depende de internet"
        assert d["veiculo"] is None
        assert vitrine.chamadas == []

    def test_api_fora_nao_derruba_a_vitrine(self, vitrine, monkeypatch):
        """No estande a falha acontece na frente do cliente: ela não pode virar erro."""
        def _explode(*a, **kw):
            raise RuntimeError("boom")
        monkeypatch.setattr(apiplacas, "consultar", _explode)
        r = vitrine.scan(forcar=True)
        assert r.status_code == 200, r.text
        assert r.json()["placa"] == VISITANTE
        assert r.json()["veiculo"] is None


class TestCarrinhoIntacto:
    """O modo livre não pode mudar nada do que já funcionava."""

    def test_mockada_continua_vindo_da_ficha_local(self, vitrine):
        d = vitrine.scan(forcar=True, placa=DEMO, mockada=True).json()
        assert d["mockada"] is True
        assert d["veiculo"]["origem"] == "feira"
        assert d["veiculo"]["combustivel"], "a ficha local alimenta o card"

    def test_carrinho_nao_gasta_nem_com_livre_ligado(self, vitrine):
        """Consultar a placa do carrinho custaria dinheiro para receber dados de OUTRO
        veículo e servi-los como se fossem do carro do estande. `bloco_de_leitura` vem
        primeiro em `_veiculo_da_vitrine` exatamente por isso."""
        vitrine.scan(forcar=True, placa=DEMO, mockada=True)
        assert vitrine.chamadas == []
