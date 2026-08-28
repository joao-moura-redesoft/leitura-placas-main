"""Consulta de dados do veículo na apiplacas.com.br e o cache que evita repagá-la.

Cada consulta à API custa crédito pré-pago, então boa parte do que se verifica aqui não é
"o dado saiu certo" e sim **"não gastamos"**. Esses casos afirmam sobre um CONTADOR de
chamadas à função de fronteira (`buscar_na_api`), nunca reexecutando a regra sob teste —
uma asserção que reimplementa a regra passa junto com o bug (já aconteceu neste repo).

Os testes de `normalizar` usam o JSON de exemplo da documentação oficial e afirmam
valores LITERAIS, pelo mesmo motivo.
"""
from __future__ import annotations

import json

import pytest

from app.core import banco, config
from app.integracoes import apiplacas
from app.seguranca import limitador

# Resposta 200 real, copiada da documentação da apiplacas (doc.php, seção "Placa").
# Reduzida nos campos que não usamos, preservada nos que usamos.
DOC = {
    "MARCA": "VW", "MODELO": "CROSSFOX", "SUBMODELO": "CROSSFOX",
    "ano": "2007", "anoModelo": "2007", "chassi": "*****10137",
    "cor": "Prata", "data": "20/07/2022 15:10:09",
    "extra": {
        "ano_fabricacao": "2007", "ano_modelo": "2007", "cilindradas": "1599",
        "combustivel": "Alcool / Gasolina", "especie": "Passageiro",
        "municipio": "SAO LEOPOLDO", "tipo_veiculo": "Automovel", "uf": "RS",
    },
    "fipe": {"dados": [{
        "ano_modelo": "2007", "codigo_fipe": "005225-6", "combustivel": "Gasolina",
        "score": 101, "sigla_combustivel": "G", "texto_valor": "R$ 28.799,00",
    }]},
    "marca": "VW", "marcaModelo": "VW/CROSSFOX", "modelo": "CROSSFOX",
    "municipio": "São Leopoldo", "placa": "INT8C36", "situacao": "Sem restrição",
    "uf": "RS",
}

PLACA = "ABC1D23"


@pytest.fixture
def cfg_ativo(ambiente, monkeypatch):
    """Recurso ligado, com token, e sem estado de disjuntor/limitador vazando entre casos."""
    config.salvar({**config.carregar(),
                   "apiplacas_ativo": "sim", "apiplacas_token": "TOKEN-SECRETO"})
    limitador._resetar_para_teste()
    apiplacas.limpar_pausa()
    yield config.carregar()
    apiplacas.limpar_pausa()


@pytest.fixture
def fronteira(monkeypatch):
    """Substitui a ÚNICA função que fala com a rede e conta as chamadas.

    Mesmo molde do stub de SMTP em `test_conta.py`: troca-se a função de fronteira, não o
    cliente HTTP. `chamadas` é a evidência dos testes de custo.
    """
    chamadas: list[str] = []
    resposta = {"valor": (200, DOC, "")}

    def _falso(placa, token, timeout_seg, base_url):
        chamadas.append(placa)
        return resposta["valor"]

    monkeypatch.setattr(apiplacas, "buscar_na_api", _falso)
    return type("F", (), {"chamadas": chamadas,
                          "responder": lambda self, v: resposta.update(valor=v)})()


# ─── Normalização (pura: sem rede, sem banco) ──────────────────────────────

class TestNormalizacao:
    def test_combustivel_sai_do_extra(self):
        """O campo que motiva a integração inteira, com o payload real da doc."""
        assert apiplacas.normalizar(DOC)["combustivel"] == "Alcool / Gasolina"

    def test_prefere_extra_a_fipe(self):
        """`extra` descreve o VEÍCULO, a FIPE descreve a versão precificada.

        Num flex a FIPE responde a pergunta errada ("Gasolina"), e flex é exatamente o que
        o posto precisa saber. As duas fontes estão presentes no payload da doc, com
        valores diferentes — este teste trava qual delas ganha.
        """
        n = apiplacas.normalizar(DOC)
        assert n["combustivel"] == "Alcool / Gasolina"   # extra
        assert DOC["fipe"]["dados"][0]["combustivel"] == "Gasolina"  # o que NÃO foi usado

    def test_extra_ausente_cai_para_fipe(self):
        """A doc avisa que `extra` pode vir ausente — é onde vive o combustível."""
        sem = {k: v for k, v in DOC.items() if k != "extra"}
        assert apiplacas.normalizar(sem)["combustivel"] == "Gasolina"

    def test_extra_nulo_nao_quebra(self):
        """Ausente e presente-porém-null são casos diferentes; `or {}` cobre os dois."""
        assert apiplacas.normalizar({**DOC, "extra": None})["combustivel"] == "Gasolina"

    def test_escolhe_a_fipe_de_maior_score(self):
        """A doc manda usar o maior `score`. O vencedor aqui é o do MEIO, então tanto
        `[0]` quanto `[-1]` falhariam este teste."""
        multi = {**DOC, "fipe": {"dados": [
            {"score": 12, "sigla_combustivel": "D"},
            {"score": 98, "sigla_combustivel": "G"},
            {"score": 40, "sigla_combustivel": "A"},
        ]}}
        assert apiplacas.normalizar(multi)["combustivel_sigla"] == "G"

    def test_score_invalido_nao_quebra(self):
        ruim = {**DOC, "fipe": {"dados": [{"sigla_combustivel": "X"},
                                          {"score": "alto", "sigla_combustivel": "Y"}]}}
        assert apiplacas.normalizar(ruim)["combustivel_sigla"] in ("X", "Y")

    def test_sem_fipe_nao_inventa_sigla(self):
        """Sigla só sai da FIPE. Derivá-la de "Alcool / Gasolina" exigiria escolher um dos
        dois combustíveis — afirmando algo que o registro não diz."""
        sem = {k: v for k, v in DOC.items() if k != "fipe"}
        n = apiplacas.normalizar(sem)
        assert n["combustivel_sigla"] is None
        assert n["combustivel"] == "Alcool / Gasolina"   # o principal sobrevive

    def test_sem_extra_nem_fipe_devolve_none(self):
        assert apiplacas.normalizar({"marca": "VW"})["combustivel"] is None

    def test_ano_string_vira_inteiro(self):
        """A API manda ano como string; o payload promete número."""
        assert apiplacas.normalizar(DOC)["ano"] == 2007

    def test_ano_lixo_vira_none(self):
        assert apiplacas.normalizar({**DOC, "ano": "-", "extra": {}})["ano"] is None

    def test_vazio_vira_none_nunca_string_vazia(self):
        """Duas representações de "não sei" no payload é o que faz o consumidor errar."""
        assert apiplacas.normalizar({**DOC, "cor": "   "})["cor"] is None

    def test_aceita_chave_em_maiuscula(self):
        """A resposta traz `MARCA` e `marca`; a doc lista as duas famílias."""
        so_maiuscula = {"MARCA": "FIAT", "extra": {}, "fipe": {}}
        assert apiplacas.normalizar(so_maiuscula)["marca"] == "FIAT"

    def test_todas_as_chaves_curadas_saem(self):
        """O bloco tem de preencher exatamente as colunas que o banco espera."""
        assert set(apiplacas.normalizar(DOC)) == set(banco.CAMPOS_CURADOS)

    @pytest.mark.parametrize("payload", [
        {"marca": "VW", "extra": [{"combustivel": "x"}]},   # objeto virou lista
        {"marca": "VW", "fipe": [{"score": 1}]},
        {"marca": "VW", "fipe": {"dados": {"score": 1}}},   # lista virou objeto
        {"marca": "VW", "fipe": {"dados": "nada"}},
        {"marca": "VW", "extra": "nada"},
    ])
    def test_tipo_errado_no_json_nao_levanta(self, payload):
        """Regressão: `x or {}` protege contra None e contra vazio, mas uma LISTA NÃO
        VAZIA passa direto e estoura no `.get` seguinte.

        Não é defensiva à toa: o schema é de terceiro e pode mudar sem aviso, e uma
        exceção aqui acontece DEPOIS de a consulta ter sido paga — o dado não é entregue
        ao posto nem gravado no cache, então o dinheiro vai embora por nada.
        """
        assert apiplacas.normalizar(payload)["marca"] == "VW"

    def test_campo_de_texto_que_virou_objeto_vira_none(self):
        """`str({"a": 1})` colocaria um repr de Python no payload entregue ao posto —
        pior que não informar nada."""
        assert apiplacas.normalizar({"marca": {"a": 1}})["marca"] is None


# ─── Placa: normalização e formato ─────────────────────────────────────────

class TestPlaca:
    @pytest.mark.parametrize("entrada", ["abc1d23", " ABC-1D23 ", "ABC1D23", "abc 1d23"])
    def test_normaliza_para_a_mesma_chave(self, entrada):
        """A comparação de TEXT PRIMARY KEY no SQLite é binária: sem normalizar, cada
        variação de caixa viraria uma linha nova e uma cobrança nova pelo mesmo veículo."""
        assert apiplacas.normalizar_placa(entrada) == "ABC1D23"

    @pytest.mark.parametrize("placa,ok", [
        ("ABC1D23", True),   # mercosul
        ("ABC1234", True),   # antigo
        ("ABC", False), ("", False), ("ABCD123", False),
    ])
    def test_consultavel(self, placa, ok):
        assert apiplacas.placa_consultavel(placa) is ok


# ─── Cache: o que a feature existe para fazer ──────────────────────────────

class TestCache:
    def test_segunda_leitura_nao_consulta_de_novo(self, cfg_ativo, fronteira):
        """O teste central: duas leituras da mesma placa, UMA chamada paga."""
        r1 = apiplacas.consultar(PLACA, cfg_ativo)
        r2 = apiplacas.consultar(PLACA, cfg_ativo)
        assert len(fronteira.chamadas) == 1
        assert r1["origem"] == "api" and r2["origem"] == "cache"
        assert r2["combustivel"] == "Alcool / Gasolina"

    def test_caixa_diferente_nao_repaga(self, cfg_ativo, fronteira):
        """Sem normalizar a chave, "abc1d23" seria uma segunda linha e uma segunda conta."""
        apiplacas.consultar(PLACA, cfg_ativo)
        apiplacas.consultar(PLACA.lower(), cfg_ativo)
        assert len(fronteira.chamadas) == 1
        assert banco.veiculos_stats()["total"] == 1

    def test_grava_o_bruto_para_nao_repagar_por_campo_novo(self, cfg_ativo, fronteira):
        """O JSON inteiro fica guardado: expor um campo novo amanhã não custa nada."""
        apiplacas.consultar(PLACA, cfg_ativo)
        bruto = json.loads(banco.veiculos_obter(PLACA)["bruto"])
        assert bruto["extra"]["cilindradas"] == "1599"   # campo que NÃO expomos hoje

    def test_ttl_vencido_reconsulta_e_soma_consulta(self, cfg_ativo, fronteira):
        apiplacas.consultar(PLACA, cfg_ativo)
        antes = banco.veiculos_obter(PLACA)
        with banco.cursor() as c:      # envelhece a linha
            c.execute("UPDATE veiculos SET consultado_em = ? WHERE placa = ?",
                      ("2020-01-01T00:00:00+00:00", PLACA))
        limitador._resetar_para_teste()   # o cooldown por placa é outro assunto
        apiplacas.consultar(PLACA, cfg_ativo)
        depois = banco.veiculos_obter(PLACA)
        assert len(fronteira.chamadas) == 2
        assert depois["consultas"] == 2
        assert depois["criado_em"] == antes["criado_em"], "histórico de gasto não pode zerar"

    def test_ttl_zero_nunca_vence(self, ambiente, fronteira):
        config.salvar({**config.carregar(), "apiplacas_ativo": "sim",
                       "apiplacas_token": "x", "apiplacas_ttl_dias": "0"})
        limitador._resetar_para_teste()
        cfg = config.carregar()
        apiplacas.consultar(PLACA, cfg)
        with banco.cursor() as c:
            c.execute("UPDATE veiculos SET consultado_em = ? WHERE placa = ?",
                      ("2000-01-01T00:00:00+00:00", PLACA))
        limitador._resetar_para_teste()
        assert apiplacas.consultar(PLACA, cfg)["origem"] == "cache"
        assert len(fronteira.chamadas) == 1

    def test_negativa_tem_prazo_proprio(self, ambiente):
        """Os dois prazos são independentes: uma negativa de 60 dias já venceu para o
        prazo negativo (30) mas ainda estaria válida pelo prazo positivo (180)."""
        banco.veiculos_salvar(PLACA, status="inexistente", campos={}, http_status=406)
        with banco.cursor() as c:
            c.execute("UPDATE veiculos SET consultado_em = ? WHERE placa = ?",
                      ("2020-01-01T00:00:00+00:00", PLACA))
        assert banco.veiculos_valido(PLACA, 180, 30) is None      # vencida
        assert banco.veiculos_valido(PLACA, 180, 0) is not None   # prazo negativo desligado

    def test_cache_e_compartilhado_entre_postos(self, cfg_ativo, fronteira):
        """A tabela não tem coluna de posto: quem consulta primeiro paga, todos aproveitam.
        Como não há escopo nenhum na chave, o cache hit independe de quem pergunta."""
        apiplacas.consultar(PLACA, cfg_ativo)
        with banco.cursor() as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(veiculos)").fetchall()}
        assert not {"empresa_id", "posto_id", "cnpj"} & cols
        assert apiplacas.consultar(PLACA, cfg_ativo)["origem"] == "cache"
        assert len(fronteira.chamadas) == 1


# ─── Desfechos da API ──────────────────────────────────────────────────────

class TestDesfechos:
    def test_406_e_cacheado_como_negativa(self, cfg_ativo, fronteira):
        """"Sem resultados" é resposta legítima. Sem cachear, uma placa que o OCR leu
        errado — e que portanto nunca vai existir — seria recomprada em todo abastecimento."""
        fronteira.responder((406, {"message": "Sem resultados"}, "Sem resultados"))
        r1 = apiplacas.consultar(PLACA, cfg_ativo)
        r2 = apiplacas.consultar(PLACA, cfg_ativo)
        assert r1["consulta"] == apiplacas.CONSULTA_INEXISTENTE
        assert len(fronteira.chamadas) == 1
        assert r2["origem"] == "cache"
        assert banco.veiculos_obter(PLACA)["status"] == "inexistente"

    def test_200_com_corpo_de_erro_nao_vira_cache(self, cfg_ativo, fronteira):
        """Regressão: API que devolve erro com status 200 envenenava o cache por 180 dias.

        O posto receberia `consulta: "ok"` com `combustivel: null` — que a doc do contrato
        manda interpretar como "o registro não informou" — quando na verdade a chamada
        falhou. E ficaria assim até o TTL vencer, sem sintoma nenhum além do combustível
        que nunca chega.
        """
        fronteira.responder((200, {"message": "Placa Invalida"}, ""))
        r = apiplacas.consultar(PLACA, cfg_ativo)
        assert r["consulta"] == apiplacas.CONSULTA_INDISPONIVEL
        assert banco.veiculos_obter(PLACA) is None

    def test_200_parcial_ainda_e_cacheado(self, cfg_ativo, fronteira):
        """O portão acima não pode ser rigoroso demais: registro incompleto é normal (a
        doc do fornecedor avisa), e um único campo já vale guardar."""
        fronteira.responder((200, {"marca": "FIAT"}, ""))
        r = apiplacas.consultar(PLACA, cfg_ativo)
        assert r["consulta"] == apiplacas.CONSULTA_OK
        assert r["marca"] == "FIAT" and r["combustivel"] is None
        assert banco.veiculos_obter(PLACA)["status"] == "ok"

    def test_timeout_nao_vira_cache(self, cfg_ativo, fronteira):
        """Falha de transporte não é resposta sobre o veículo. Gravá-la faria um minuto
        ruim do fornecedor marcar a placa como inexistente por 30 dias."""
        fronteira.responder((None, None, "ReadTimeout"))
        r = apiplacas.consultar(PLACA, cfg_ativo)
        assert r["consulta"] == apiplacas.CONSULTA_INDISPONIVEL
        assert banco.veiculos_obter(PLACA) is None

    def test_429_nao_grava_e_pausa_todas_as_placas(self, cfg_ativo, fronteira):
        """Sem saldo não se resolve com retry — insistir só cobra latência de cada
        abastecimento. A pausa é global, então outra placa também é barrada."""
        fronteira.responder((429, {"message": "Limite"}, "Limite"))
        apiplacas.consultar(PLACA, cfg_ativo)
        apiplacas.consultar("XYZ9W88", cfg_ativo)
        assert len(fronteira.chamadas) == 1
        assert banco.veiculos_obter(PLACA) is None

    def test_402_pausa_e_limpar_pausa_libera(self, cfg_ativo, fronteira):
        """`limpar_pausa` é o que faz corrigir o token no painel surtir efeito na hora."""
        fronteira.responder((402, {"message": "Token"}, "Token"))
        apiplacas.consultar(PLACA, cfg_ativo)
        apiplacas.consultar("XYZ9W88", cfg_ativo)
        assert len(fronteira.chamadas) == 1
        apiplacas.limpar_pausa()
        limitador._resetar_para_teste()
        fronteira.responder((200, DOC, ""))
        assert apiplacas.consultar("XYZ9W88", cfg_ativo)["consulta"] == "ok"
        assert len(fronteira.chamadas) == 2

    def test_tres_falhas_seguidas_abrem_o_disjuntor(self, cfg_ativo, fronteira):
        """Falha isolada não vale pausa; o fornecedor fora por horas, sim."""
        fronteira.responder((None, None, "ReadTimeout"))
        for placa in ["AAA1A11", "BBB2B22", "CCC3C33", "DDD4D44"]:
            apiplacas.consultar(placa, cfg_ativo)
        assert len(fronteira.chamadas) == 3, "a 4ª deve ser barrada pelo disjuntor"

    def test_sucesso_zera_o_contador_de_falhas(self, cfg_ativo, fronteira):
        fronteira.responder((None, None, "ReadTimeout"))
        apiplacas.consultar("AAA1A11", cfg_ativo)
        apiplacas.consultar("BBB2B22", cfg_ativo)
        fronteira.responder((200, DOC, ""))
        apiplacas.consultar("CCC3C33", cfg_ativo)     # sucesso zera
        fronteira.responder((None, None, "ReadTimeout"))
        apiplacas.consultar("DDD4D44", cfg_ativo)
        apiplacas.consultar("EEE5E55", cfg_ativo)
        assert len(fronteira.chamadas) == 5, "sem pausa: o contador foi zerado no sucesso"

    def test_bloco_tem_sempre_as_mesmas_chaves(self, cfg_ativo, fronteira):
        """Um bloco que muda de forma é o pior caso para o sidecar Java tipado."""
        fronteira.responder((200, DOC, ""))
        ok = apiplacas.consultar(PLACA, cfg_ativo)
        fronteira.responder((406, {}, ""))
        inexistente = apiplacas.consultar("XYZ9W88", cfg_ativo)
        fronteira.responder((None, None, "erro"))
        indisponivel = apiplacas.consultar("QQQ1Q11", cfg_ativo)
        assert set(ok) == set(inexistente) == set(indisponivel) == set(apiplacas.CHAVES_VEICULO)

    def test_nenhum_curado_vem_string_vazia(self, cfg_ativo, fronteira):
        veiculo = apiplacas.consultar(PLACA, cfg_ativo)
        assert all(veiculo[k] != "" for k in banco.CAMPOS_CURADOS)


# ─── Portões que impedem gasto ─────────────────────────────────────────────

class TestNaoGasta:
    def test_cache_only_nunca_chama_a_api(self, cfg_ativo, fronteira):
        r = apiplacas.consultar(PLACA, cfg_ativo, permitir_gasto=False)
        assert fronteira.chamadas == []
        assert r["consulta"] == apiplacas.CONSULTA_INDISPONIVEL

    def test_cache_only_ainda_serve_o_que_ja_foi_pago(self, cfg_ativo, fronteira):
        apiplacas.consultar(PLACA, cfg_ativo)                      # paga uma vez
        r = apiplacas.consultar(PLACA, cfg_ativo, permitir_gasto=False)
        assert r["origem"] == "cache" and r["combustivel"] == "Alcool / Gasolina"
        assert len(fronteira.chamadas) == 1

    def test_placa_invalida_nem_chega_na_api(self, cfg_ativo, fronteira):
        """Evita gastar para receber HTTP 401 "placa inválida"."""
        assert apiplacas.consultar("ABC", cfg_ativo)["consulta"] == "indisponivel"
        assert fronteira.chamadas == []

    def test_desligado_nao_chama(self, cfg_ativo, fronteira):
        apiplacas.consultar(PLACA, {**cfg_ativo, "apiplacas_ativo": "nao"})
        assert fronteira.chamadas == []

    def test_sem_token_nao_chama(self, cfg_ativo, fronteira):
        apiplacas.consultar(PLACA, {**cfg_ativo, "apiplacas_token": ""})
        assert fronteira.chamadas == []

    def test_sem_orcamento_de_tempo_nao_chama(self, cfg_ativo, fronteira):
        """A leitura já queimou o tempo; consultar empurraria a resposta para além do que
        o roteador tolera. O dado aparece na próxima leitura, do cache."""
        apiplacas.consultar(PLACA, cfg_ativo, orcamento_seg=0.05)
        assert fronteira.chamadas == []

    def test_desistir_por_falta_de_tempo_nao_queima_o_cooldown(self, cfg_ativo, fronteira):
        """Regressão: `limitador.permitido` CONSOME o slot ao devolver True.

        Enquanto os portões consumidores rodavam antes do portão de orçamento, uma consulta
        abortada por falta de tempo — que não gastou centavo nenhum — queimava o cooldown de
        10 minutos daquela placa. A leitura seguinte, com tempo de sobra, era recusada com
        "placa já consultada há instantes" sem a placa nunca ter sido consultada: o posto
        ficava até 10 min sem o combustível, de graça e em silêncio.
        """
        sem_tempo = apiplacas.consultar(PLACA, cfg_ativo, orcamento_seg=0.05)
        assert sem_tempo["consulta"] == apiplacas.CONSULTA_INDISPONIVEL
        assert fronteira.chamadas == []

        # Mesma placa, agora com orçamento normal: tem de consultar de verdade.
        r = apiplacas.consultar(PLACA, cfg_ativo)
        assert fronteira.chamadas == [PLACA]
        assert r["consulta"] == apiplacas.CONSULTA_OK

    def test_teto_diario_nao_queima_o_cooldown(self, ambiente, fronteira):
        """Mesma armadilha do teste acima, pelo outro portão que só lê estado."""
        config.salvar({**config.carregar(), "apiplacas_ativo": "sim",
                       "apiplacas_token": "x", "apiplacas_max_por_dia": "1"})
        limitador._resetar_para_teste()
        apiplacas.limpar_pausa()
        cfg = config.carregar()

        apiplacas.consultar("ZZZ1Z11", cfg)          # consome a cota do dia
        barrada = apiplacas.consultar(PLACA, cfg)    # barrada pelo teto, sem gastar
        assert barrada["consulta"] == apiplacas.CONSULTA_INDISPONIVEL
        assert len(fronteira.chamadas) == 1

        # Teto ampliado: a placa barrada tem de poder ser consultada na hora.
        cfg = {**cfg, "apiplacas_max_por_dia": "10"}
        apiplacas.consultar(PLACA, cfg)
        assert fronteira.chamadas == ["ZZZ1Z11", PLACA]

    def test_retry_do_roteador_nao_repaga(self, cfg_ativo, fronteira):
        """O roteador chega a chamar 3x em ~140s no mesmo abastecimento. Com a resposta
        falhando (nada vai para o cache), só o cooldown por placa segura a recompra."""
        fronteira.responder((None, None, "ReadTimeout"))
        for _ in range(3):
            apiplacas.consultar(PLACA, cfg_ativo)
        assert len(fronteira.chamadas) == 1

    def test_teto_diario_barra(self, ambiente, fronteira):
        config.salvar({**config.carregar(), "apiplacas_ativo": "sim",
                       "apiplacas_token": "x", "apiplacas_max_por_dia": "1"})
        limitador._resetar_para_teste()
        apiplacas.limpar_pausa()
        cfg = config.carregar()
        apiplacas.consultar(PLACA, cfg)
        r = apiplacas.consultar("XYZ9W88", cfg)
        assert len(fronteira.chamadas) == 1
        assert "dia" in r["motivo"]


# ─── Segredo ───────────────────────────────────────────────────────────────

class TestToken:
    def test_token_nao_vaza_no_log(self, cfg_ativo, caplog, monkeypatch):
        """O token vai NO PATH da URL (`/consulta/{placa}/{token}`), então o reflexo
        natural — logar a URL no `except` — vazaria a credencial paga para o arquivo de
        log, de onde ela sai em qualquer suporte, backup ou colagem de diagnóstico."""
        def _explode(placa, token, timeout_seg, base_url):
            raise RuntimeError("conexão recusada")
        # Chama a fronteira DE VERDADE (é ela que monta e loga a URL), com o requests
        # substituído por algo que falha.
        import app.integracoes.apiplacas as mod
        monkeypatch.setattr(mod, "_url_segura", mod._url_segura)  # explícito: não é stub

        class _FakeRequests:
            @staticmethod
            def get(url, timeout=None):
                raise RuntimeError("conexão recusada")

        monkeypatch.setitem(__import__("sys").modules, "requests", _FakeRequests)
        with caplog.at_level("WARNING"):
            mod.buscar_na_api(PLACA, "TOKEN-SECRETO", 2.0, "https://exemplo.test")
        assert "TOKEN-SECRETO" not in caplog.text
        assert "***" in caplog.text

    def test_url_segura_mascara_o_ultimo_segmento(self):
        mascarada = apiplacas._url_segura("https://x.test/consulta/ABC1D23/SEGREDO")
        assert "SEGREDO" not in mascarada
        assert mascarada.endswith("/***")
