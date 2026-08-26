"""O bloco `veiculo` no payload que o roteador do posto recebe.

Duas coisas são verificadas aqui, e a segunda é a que custa dinheiro se regredir:

1. o combustível chega ao roteador, e a resposta continua saindo mesmo com a API fora;
2. os fluxos que NÃO podem gastar (botão de teste, painel, leitura não confirmada)
   realmente não chamam a API paga.

Como em `test_apiplacas.py`, os casos de custo afirmam sobre um contador de chamadas à
função de fronteira — a evidência direta de que não se gastou — em vez de reexecutar a
regra que decide gastar.

`ler_placa` é substituída por um dublê: a câmera da fixture `posto` não existe, e uma
leitura real devolveria 503 antes de chegar ao que este arquivo testa.
"""
from __future__ import annotations

import pytest

from app.core import banco, config
from app.integracoes import apiplacas
from app.seguranca import limitador
from app.web import leitura as leitura_rotas

# Recorte da resposta 200 real da doc da apiplacas. Duplicado de `test_apiplacas.py` de
# propósito: não há `__init__.py` em `testes/unitarios/` e nenhum outro teste do repo
# importa de outro módulo de teste — a cópia curta custa menos que o acoplamento.
DOC = {
    "marca": "VW", "modelo": "CROSSFOX", "ano": "2007", "anoModelo": "2007",
    "cor": "Prata", "municipio": "São Leopoldo", "uf": "RS", "situacao": "Sem restrição",
    "extra": {"combustivel": "Alcool / Gasolina", "especie": "Passageiro",
              "tipo_veiculo": "Automovel", "cilindradas": "1599"},
    "fipe": {"dados": [{"score": 101, "sigla_combustivel": "G",
                        "combustivel": "Gasolina"}]},
}

PLACA = "ABC1D23"


def _resultado(**over) -> dict:
    """Retorno canônico de `ler_placa` no caminho de sucesso."""
    base = {
        "camera_id": 7, "bico_id": 1, "placa": PLACA, "padrao": "mercosul",
        "confianca": 0.91, "votos_snapshot": 5, "total_snapshots": 6, "votos_ocr": 2,
        "total_engines": 2, "detalhes_ocr": [], "snapshot": None, "frame_url": None,
        "tentativas": 6, "acordo": 0.85, "confirmada": True, "parada_motivo": "acordo",
        "tipo_veiculo": "carro", "n_cameras_votando": 1, "fontes": [], "avisos": [],
    }
    base.update(over)
    return base


@pytest.fixture
def cenario(ambiente, admin, posto, monkeypatch):
    """Recurso ligado, `ler_placa` dublada e a fronteira HTTP contada."""
    # `automatico` explícito: o PADRÃO do sistema é `manual` (nada consulta sozinho), e a
    # maior parte deste arquivo exercita justamente o caminho em que o abastecimento
    # consulta. Os testes do modo manual trocam a chave de volta, em `TestModo`.
    config.salvar({**config.carregar(), "apiplacas_ativo": "sim",
                   "apiplacas_modo": "automatico", "apiplacas_token": "TOKEN-SECRETO"})
    limitador._resetar_para_teste()
    apiplacas.limpar_pausa()

    estado = {"resultado": _resultado(), "resposta": (200, DOC, "")}
    chamadas: list[str] = []

    monkeypatch.setattr(leitura_rotas.leitura, "ler_placa",
                        lambda **kw: dict(estado["resultado"]))

    def _fronteira(placa, token, timeout_seg, base_url):
        chamadas.append(placa)
        return estado["resposta"]

    monkeypatch.setattr(apiplacas, "buscar_na_api", _fronteira)

    def _ler(**over):
        if over:
            estado["resultado"] = _resultado(**over)
        return admin.get("/api/leitura", params={
            "entidade": "Rede Teste", "cnpj": posto["cnpj"],
            "automacao": "1", "bico": "3",
        })

    yield type("C", (), {
        "ler": staticmethod(_ler),
        "chamadas": chamadas,
        "responder": staticmethod(lambda v: estado.update(resposta=v)),
        "posto": posto,
        "admin": admin,
    })()
    apiplacas.limpar_pausa()


class TestPayload:
    def test_combustivel_chega_ao_roteador(self, cenario):
        """O teste ponta a ponta da feature."""
        r = cenario.ler()
        assert r.status_code == 200, r.text
        v = r.json()["veiculo"]
        assert v["combustivel"] == "Alcool / Gasolina"
        assert v["combustivel_sigla"] == "G"
        assert v["consulta"] == "ok" and v["origem"] == "api"

    def test_segunda_leitura_vem_do_cache(self, cenario):
        cenario.ler()
        v = cenario.ler().json()["veiculo"]
        assert v["origem"] == "cache"
        assert v["combustivel"] == "Alcool / Gasolina"
        assert len(cenario.chamadas) == 1, "duas leituras, uma consulta paga"

    def test_placa_continua_intacta(self, cenario):
        """O enriquecimento é aditivo: nada do contrato antigo pode mudar."""
        corpo = cenario.ler().json()
        assert corpo["placa"] == PLACA
        assert corpo["confirmada"] is True
        assert corpo["acordo"] == 0.85

    def test_desligado_nao_altera_o_payload(self, cenario):
        """Quem não usa a feature tem o payload de hoje, byte por byte."""
        config.salvar({**config.carregar(), "apiplacas_ativo": "nao"})
        corpo = cenario.ler().json()
        assert "veiculo" not in corpo
        assert cenario.chamadas == []

    def test_sem_placa_nao_ganha_bloco(self, cenario):
        """Preserva o conjunto de chaves do caminho "não leu"."""
        corpo = cenario.ler(placa=None, mensagem="nada detectado").json()
        assert "veiculo" not in corpo
        assert cenario.chamadas == []


class TestNuncaQuebraALeitura:
    def test_api_fora_devolve_a_placa_normalmente(self, cenario):
        """O caso mais importante do arquivo: a leitura é o produto, a consulta é enfeite."""
        cenario.responder((None, None, "ReadTimeout"))
        r = cenario.ler()
        assert r.status_code == 200
        corpo = r.json()
        assert corpo["placa"] == PLACA
        assert corpo["veiculo"]["consulta"] == "indisponivel"
        assert corpo["veiculo"]["combustivel"] is None
        assert banco.veiculos_obter(PLACA) is None, "falha de rede não vira cache"

    def test_excecao_inesperada_nao_derruba(self, cenario, monkeypatch):
        def _explode(*a, **kw):
            raise RuntimeError("boom")
        monkeypatch.setattr(apiplacas, "consultar", _explode)
        r = cenario.ler()
        assert r.status_code == 200
        assert r.json()["placa"] == PLACA

    def test_indisponibilidade_vai_para_o_motivo_sem_falsear_o_status(self, cenario):
        """"O crédito acabou" precisa aparecer onde o operador já olha — mas a leitura foi
        boa, e rebaixar o status falsearia a taxa de sucesso do painel."""
        cenario.responder((429, {"message": "Limite"}, "Limite"))
        cenario.ler()
        ch = banco.chamadas_listar(limit=1)[0]
        assert ch["status"] == "ok"
        assert "veiculo:" in ch["motivo"]


class TestModo:
    """`apiplacas_modo` decide QUEM inicia uma consulta paga.

    Com cota curta, o abastecimento não pode gastar sozinho — mas também não pode parar de
    entregar a placa, que é o produto. Estes testes travam as duas metades.
    """

    def test_o_padrao_do_sistema_e_manual(self):
        """Atualizar o sistema não pode fazer ninguém começar a gastar."""
        assert config.PADROES["apiplacas_modo"] == "manual"

    def test_manual_nao_gasta_no_abastecimento(self, cenario):
        config.salvar({**config.carregar(), "apiplacas_modo": "manual"})
        corpo = cenario.ler().json()
        assert cenario.chamadas == [], "em manual, o abastecimento não consulta"
        assert corpo["veiculo"]["consulta"] == "indisponivel"
        assert corpo["placa"] == PLACA, "a leitura continua entregando a placa"
        assert corpo["confirmada"] is True
        assert banco.veiculos_obter(PLACA) is None

    def test_manual_ainda_serve_o_que_ja_foi_pago(self, cenario):
        """O modo governa quem GASTA, não quem lê o cache — senão o dado já comprado
        ficaria inacessível justamente quando a cota está curta."""
        cenario.ler()                                    # em automatico, paga uma vez
        config.salvar({**config.carregar(), "apiplacas_modo": "manual"})
        limitador._resetar_para_teste()
        corpo = cenario.ler().json()
        assert corpo["veiculo"]["origem"] == "cache"
        assert corpo["veiculo"]["combustivel"] == "Alcool / Gasolina"
        assert len(cenario.chamadas) == 1

    def test_automatico_gasta(self, cenario):
        cenario.ler()
        assert len(cenario.chamadas) == 1

    def test_valor_desconhecido_nao_gasta(self, cenario):
        """Fail-safe: qualquer coisa que não seja exatamente `automatico` é tratada como
        manual. Um typo na config (`automático`, com acento) não pode virar gasto."""
        config.salvar({**config.carregar(), "apiplacas_modo": "automático"})
        cenario.ler()
        assert cenario.chamadas == []


class TestNaoGasta:
    def test_leitura_nao_confirmada_nao_gasta(self, cenario):
        """Pode ser a placa errada — ou placa nenhuma. Pagar por ela é gasto certo por
        benefício nenhum, e ainda polui o cache com uma placa que não existe."""
        corpo = cenario.ler(confirmada=False, acordo=0.4).json()
        assert cenario.chamadas == []
        assert corpo["veiculo"]["consulta"] == "indisponivel"
        assert corpo["placa"] == PLACA, "a placa continua sendo entregue"

    def test_timeout_do_laco_nao_gasta(self, cenario):
        """Sair por timeout significa que o consenso nunca fechou, mesmo sem `confirmada`
        vir False — é o caso que consome os 28s e não vale enriquecer."""
        cenario.ler(parada_motivo="timeout", confirmada=None)
        assert cenario.chamadas == []

    def test_nao_confirmada_ainda_mostra_o_que_ja_foi_pago(self, cenario):
        """Cache-only: o atendente vê marca/modelo na placa duvidosa, de graça."""
        cenario.ler()                                   # paga uma vez, confirmada
        limitador._resetar_para_teste()
        corpo = cenario.ler(confirmada=False).json()
        assert corpo["veiculo"]["origem"] == "cache"
        assert corpo["veiculo"]["combustivel"] == "Alcool / Gasolina"
        assert len(cenario.chamadas) == 1

    def test_exigir_confirmada_desligado_gasta(self, cenario):
        config.salvar({**config.carregar(), "apiplacas_exigir_confirmada": "nao"})
        cenario.ler(confirmada=False)
        assert len(cenario.chamadas) == 1


class TestBotaoDeTesteNaoGasta:
    def test_ajustar_roi_nao_consome_credito(self, cenario, monkeypatch):
        """O botão "Testar como o roteador" e o editor de ROI são clicados em rajada
        enquanto se ajusta o enquadramento. É a regressão mais cara possível desta feature:
        um gancho dentro de `ler_placa` faria cada clique custar uma consulta."""
        from app.web import cadastro as cadastro_rotas
        monkeypatch.setattr(cadastro_rotas.leitura, "ler_placa", lambda **kw: _resultado())
        bico = cenario.posto["bico_id"]
        for _ in range(5):
            r = cenario.admin.post(f"/api/bicos/{bico}/ler-placa-teste")
            assert r.status_code == 200, r.text
        assert cenario.chamadas == [], "cinco cliques, zero consultas pagas"
        assert r.json()["veiculo"]["consulta"] == "indisponivel"

    def test_teste_mostra_o_cache_quando_existe(self, cenario, monkeypatch):
        cenario.ler()                                   # paga uma vez pela rota do roteador
        from app.web import cadastro as cadastro_rotas
        monkeypatch.setattr(cadastro_rotas.leitura, "ler_placa", lambda **kw: _resultado())
        r = cenario.admin.post(f"/api/bicos/{cenario.posto['bico_id']}/ler-placa-teste")
        assert r.json()["veiculo"]["origem"] == "cache"
        assert len(cenario.chamadas) == 1

    def test_painel_de_placa_nao_gasta(self, cenario):
        r = cenario.admin.get(f"/api/placa/{PLACA}")
        assert r.status_code == 200
        assert cenario.chamadas == []


class TestDecisaoDeGasto:
    """`_pode_gastar` é pura de propósito — é a regra que gasta dinheiro do cliente.

    Aqui o que se isola é a regra de CONSENSO (confirmada/parada_motivo). O portão do modo
    é outro assunto e tem sua própria classe (`TestModo`), então o cfg destes casos fica em
    `automatico` — senão todos passariam pelo motivo errado, dando falso verde para
    qualquer bug na regra de consenso.
    """

    @pytest.fixture
    def cfg(self, ambiente):
        return {**config.carregar(), "apiplacas_modo": "automatico"}

    def test_sem_placa_nao_gasta(self, cfg):
        assert leitura_rotas._pode_gastar({"placa": None}, cfg) is False

    def test_confirmada_gasta(self, cfg):
        assert leitura_rotas._pode_gastar(_resultado(), cfg) is True

    def test_nao_confirmada_nao_gasta(self, cfg):
        assert leitura_rotas._pode_gastar(_resultado(confirmada=False), cfg) is False

    def test_confirmada_desconhecida_nao_bloqueia(self, cfg):
        """None é consenso DESCONHECIDO, não consenso fraco — mesma distinção que
        `_status_da_leitura` faz. Origens que não passam pelo laço caem aqui."""
        assert leitura_rotas._pode_gastar(
            _resultado(confirmada=None, parada_motivo="acordo"), cfg) is True

    def test_timeout_nao_gasta_mesmo_sem_confirmada_false(self, cfg):
        assert leitura_rotas._pode_gastar(
            _resultado(confirmada=None, parada_motivo="timeout"), cfg) is False


class TestSaldo:
    def test_admin_ve_saldo(self, cenario, monkeypatch):
        monkeypatch.setattr(apiplacas, "saldo", lambda cfg=None: 3500)
        r = cenario.admin.get("/api/apiplacas/saldo")
        assert r.status_code == 200 and r.json()["qtd_consultas"] == 3500

    def test_uso_mostra_o_que_o_cache_guarda(self, cenario):
        cenario.ler()
        u = cenario.admin.get("/api/apiplacas/uso").json()
        assert u["total"] == 1 and u["consultas"] == 1
        assert u["gasto_estimado"] == pytest.approx(0.03)

    def test_cliente_nao_ve_saldo(self, cenario, cliente_logado):
        assert cliente_logado.get("/api/apiplacas/saldo").status_code == 403


class TestSegredo:
    def test_token_e_mascarado_no_get(self, cenario):
        cfg = cenario.admin.get("/api/config").json()
        assert cfg["apiplacas_token"] == "", "token pago não pode sair em texto claro"

    def test_post_vazio_nao_apaga_o_token(self, cenario):
        cenario.admin.post("/api/config", json={"apiplacas_token": ""})
        assert config.carregar()["apiplacas_token"] == "TOKEN-SECRETO"
