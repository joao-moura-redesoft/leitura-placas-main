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
        # `mockada` sai do `ler_placa` real desde 04/09/2026. O dublê a declara para não
        # mentir por OMISSÃO: sem ela, `bloco_veiculo` nunca veria leitura mockada e a
        # suíte passaria com o gancho do modo feira inteiro morto.
        "mockada": False,
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

    def _ler(_rapido: bool = False, **over):
        """`_rapido` sai do `**over` de propósito: é query da ROTA, não campo do resultado
        de `ler_placa`. Misturar os dois faria `rapido=1` virar chave inventada no dublê e
        o teste passaria sem nunca ter pedido o perfil leve."""
        if over:
            estado["resultado"] = _resultado(**over)
        params = {"entidade": "Rede Teste", "cnpj": posto["cnpj"],
                  "automacao": "1", "bico": "3"}
        if _rapido:
            params["rapido"] = 1
        return admin.get("/api/leitura", params=params)

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


class TestModoRapidoConsulta:
    """`rapido=1` consulta igual ao modo completo (mudança de 05/09/2026).

    Antes o perfil rápido era cache-only CATEGÓRICO: um `return` antes de `_pode_gastar`
    que recusava a consulta mesmo com a leitura perfeita. O caso real que derrubou a regra
    foi um payload de moto com `acordo=1,0`, três engines devolvendo a mesma placa e
    `confirmada=True`, que saía com "sem dados em cache (este fluxo não consulta a API
    paga)" — exatamente a leitura que vale enriquecer.

    Nenhum teste cobria o veto quando ele foi removido: a suíte inteira passava nos DOIS
    estados. Esta classe existe para que a próxima mudança de opinião sobre o assunto seja
    uma decisão, e não um efeito colateral silencioso.
    """

    def test_rapido_consulta_quando_a_leitura_e_boa(self, cenario):
        """O caso que motivou a mudança, com os números do payload real."""
        corpo = cenario.ler(_rapido=True, confirmada=True, acordo=1.0,
                            parada_motivo="acordo", tipo_veiculo="moto").json()
        assert cenario.chamadas == [PLACA], "leitura boa no rápido: consulta acontece"
        assert corpo["veiculo"]["consulta"] == "ok"
        assert corpo["veiculo"]["combustivel"] == "Alcool / Gasolina"

    def test_rapido_e_completo_decidem_igual(self, cenario):
        """O perfil deixou de ser variável da decisão — é isto que a mudança afirma."""
        cenario.ler(_rapido=True)
        rapido = len(cenario.chamadas)
        banco.veiculos_remover(PLACA)                  # senão o 2º vem do cache e não mede
        limitador._resetar_para_teste()
        cenario.ler(_rapido=False)
        assert rapido == 1 and len(cenario.chamadas) == 2, "mesma leitura, mesma decisão"

    def test_rapido_nao_confirmada_continua_sem_gastar(self, cenario):
        """A trava que IMPORTA sobrevive: quem barra é `_pode_gastar`, não o perfil.

        Sem esta asserção a mudança teria trocado um veto grosso por nenhum veto."""
        corpo = cenario.ler(_rapido=True, confirmada=False, acordo=0.4).json()
        assert cenario.chamadas == []
        assert corpo["veiculo"]["consulta"] == "indisponivel"

    def test_rapido_respeita_modo_manual(self, cenario):
        """O outro interruptor de quem quiser o rápido barato de novo."""
        config.salvar({**config.carregar(), "apiplacas_modo": "manual"})
        cenario.ler(_rapido=True)
        assert cenario.chamadas == []

    def test_rapido_sem_orcamento_nao_gasta(self, cenario, monkeypatch):
        """Custo de TEMPO é tratado por `orcamento_seg`, e ele corta ANTES de gastar.

        É a razão sobrevivente do veto antigo: a consulta não pode empurrar a resposta
        para além do que o roteador tolera. Com o orçamento estourado a leitura sai
        normalmente e ninguém paga."""
        monkeypatch.setattr(leitura_rotas.config, "get_float",
                            lambda cfg, chave: 0.0
                            if chave == "apiplacas_timeout_seg"
                            else config.get_float(cfg, chave))
        corpo = cenario.ler(_rapido=True).json()
        assert cenario.chamadas == []
        assert corpo["placa"] == PLACA, "a leitura é o produto; a consulta é enfeite"
        assert corpo["veiculo"]["consulta"] == "indisponivel"


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
        from app.web import redacao
        cfg = cenario.admin.get("/api/config").json()
        assert cfg["apiplacas_token"] == redacao.MASCARA, \
            "token pago não pode sair em texto claro"

    def test_post_com_mascara_nao_apaga_o_token(self, cenario):
        """Achado A7: a tela reenvia a MÁSCARA quando não mexeu no campo (não mais vazio)."""
        from app.web import redacao
        cenario.admin.post("/api/config", json={"apiplacas_token": redacao.MASCARA})
        assert config.carregar()["apiplacas_token"] == "TOKEN-SECRETO"

    def test_post_vazio_agora_apaga_o_token_de_proposito(self, cenario):
        cenario.admin.post("/api/config", json={"apiplacas_token": ""})
        assert config.carregar()["apiplacas_token"] == ""


class TestModoFeiraOffline:
    """O bloco `veiculo` no payload quando a leitura foi MOCKADA e não há internet.

    O cenário real é o estande: sem rede, sem token e cache vazio. A consulta de verdade
    só saberia devolver `indisponivel`, e o payload da demo sairia sem combustível — que é
    o campo que esta integração existe para entregar e o que se está demonstrando.

    A evidência que importa em cada caso: `cenario.chamadas` (a fronteira HTTP) vazia é a
    prova de que o bloco veio da ficha local e não de rede nem de crédito.
    """

    DEMO = "MOK3H92"

    def test_bloco_sai_com_a_apiplacas_desligada(self, cenario):
        """O caso da feira: recurso desligado (não há token nem rede) e o bloco vem igual.

        É o requisito inteiro em um teste — se só este passar, a demo funciona.
        """
        config.salvar({**config.carregar(), "apiplacas_ativo": "nao"})
        r = cenario.ler(placa=self.DEMO, mockada=True)
        v = r.json()["veiculo"]
        assert v["consulta"] == "ok"
        assert v["combustivel"] == "Flex"
        assert v["modelo"] and v["cor"] == "Cinza"
        assert cenario.chamadas == []

    def test_forma_identica_a_da_consulta_real(self, cenario):
        """Mesmas chaves que o sidecar Java já recebe em produção — nem uma a mais."""
        config.salvar({**config.carregar(), "apiplacas_ativo": "nao"})
        v = cenario.ler(placa=self.DEMO, mockada=True).json()["veiculo"]
        assert set(v) == set(apiplacas.CHAVES_VEICULO)

    def test_nao_gasta_credito_nem_com_a_api_ligada(self, cenario):
        """Consultar a placa do carrinho custaria dinheiro para receber dados de OUTRO
        veículo (ou nada) e servi-los como se fossem do carro do estande.
        """
        v = cenario.ler(placa=self.DEMO, mockada=True).json()["veiculo"]
        assert cenario.chamadas == []
        assert v["origem"] == "feira"

    def test_leitura_real_continua_consultando(self, cenario):
        """O contraponto: sem mock, nada muda. A placa do visitante segue o caminho pago."""
        r = cenario.ler(placa=PLACA, mockada=False)
        v = r.json()["veiculo"]
        assert cenario.chamadas == [PLACA]
        assert v["origem"] == "api"
        assert v["combustivel"] == "Alcool / Gasolina"

    def test_payload_declara_que_a_leitura_foi_mockada(self, cenario):
        """`mockada` no nível de cima: o único sinal antes disto era prosa em `avisos`."""
        r = cenario.ler(placa=self.DEMO, mockada=True)
        assert r.json()["mockada"] is True

    def test_placa_mockada_sem_ficha_vira_motivo_da_chamada(self, cenario):
        """Bloco `indisponivel` é promovido ao motivo pelo caminho que já existia — assim
        "esqueci de preencher a ficha" aparece no painel em vez de na feira.
        """
        config.salvar({**config.carregar(), "apiplacas_ativo": "nao"})
        r = cenario.ler(placa="ZZZ9Z99", mockada=True)
        assert r.json()["veiculo"]["consulta"] == "indisponivel"
        motivo = banco.chamadas_listar(limit=1)[0]["motivo"] or ""
        assert "ficha" in motivo.lower()
