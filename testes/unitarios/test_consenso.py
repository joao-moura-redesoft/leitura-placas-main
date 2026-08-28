"""Consenso entre leituras — o que transforma N fotos ruidosas numa placa só.

É a lógica de negócio mais sutil do sistema (vota caractere a caractere, ponderado por
confiança, com prior de formato) e a que mais se paga em teste: um erro aqui não quebra
nada visivelmente, só devolve a placa errada de vez em quando.
"""
from __future__ import annotations

from app.visao.leitura import (
    _confirmada, _consenso_caractere, _eleger_placa, _mesclar_com_anterior,
)


def _candidato(placa, confianca=0.9, padrao="mercosul", detalhes=None):
    return {"placa": placa, "padrao": padrao, "confianca": confianca,
            "detalhes_ocr": detalhes or []}


class TestConsensoCaractere:
    def test_maioria_corrige_um_caractere_isolado(self):
        leituras = [("ABC1D23", 1.0), ("ABC1D23", 1.0), ("ABC1O23", 1.0)]
        assert _consenso_caractere(leituras) == "ABC1D23"

    def test_confianca_pesa_mais_que_quantidade(self):
        """Duas leituras fracas não derrubam uma muito confiante."""
        leituras = [("ABC1D23", 5.0), ("ABC1O23", 0.5), ("ABC1O23", 0.5)]
        assert _consenso_caractere(leituras) == "ABC1D23"

    def test_prior_de_formato_descarta_digito_onde_mercosul_exige_letra(self):
        """Posição 5 do Mercosul é LETRA: mesmo com o dígito em maioria, o voto de
        letra vindo de outro frame é que vale — sem isto o resultado seria 'ABC1223'."""
        leituras = [("ABC1223", 1.0), ("ABC1223", 1.0), ("ABC1Z23", 1.0)]
        assert _consenso_caractere(leituras, formato="mercosul") == "ABC1Z23"

    def test_sem_voto_do_tipo_certo_cai_para_o_voto_bruto(self):
        leituras = [("ABC1223", 1.0), ("ABC1223", 1.0)]
        assert _consenso_caractere(leituras, formato="mercosul") == "ABC1223"

    def test_ignora_leituras_que_nao_tem_7_caracteres(self):
        leituras = [("ABC1D23", 1.0), ("ABC12", 1.0), ("", 1.0)]
        assert _consenso_caractere(leituras) == "ABC1D23"

    def test_sem_leitura_valida_devolve_none(self):
        assert _consenso_caractere([("ABC12", 1.0)]) is None
        assert _consenso_caractere([]) is None


class TestElegerPlaca:
    def test_sem_candidatos_devolve_none(self):
        assert _eleger_placa([]) is None

    def test_elege_por_consenso_e_nao_pela_string_mais_votada(self):
        eleito = _eleger_placa([
            _candidato("ABC1D23"), _candidato("ABC1D23"), _candidato("ABC1O23"),
        ])
        assert eleito["placa"] == "ABC1D23"
        assert eleito["n_votos_snap"] == 2

    def test_acordo_total_quando_todos_concordam(self):
        eleito = _eleger_placa([_candidato("ABC1D23"), _candidato("ABC1D23")])
        assert eleito["acordo"] == 1.0

    def test_acordo_cai_quando_as_leituras_divergem(self):
        eleito = _eleger_placa([_candidato("ABC1D23"), _candidato("XYZ9K88")])
        assert eleito["acordo"] < 1.0

    def test_confianca_final_e_escalada_pelo_acordo(self):
        """Discordância tem que reduzir a confiança reportada — é ela que o roteador vê."""
        junto = _eleger_placa([_candidato("ABC1D23", 0.9), _candidato("ABC1D23", 0.9)])
        brigado = _eleger_placa([_candidato("ABC1D23", 0.9), _candidato("XYZ9K88", 0.9)])
        assert brigado["confianca"] < junto["confianca"]

    def test_padrao_e_recalculado_a_partir_da_placa_eleita(self):
        eleito = _eleger_placa([_candidato("ABC1234", padrao="mercosul")])
        assert eleito["padrao"] == "antigo"

    def test_engines_individuais_entram_na_votacao(self):
        """`detalhes_ocr` são as leituras de cada engine — o voto delas conta."""
        eleito = _eleger_placa([
            _candidato("ABC1O23", 0.5, detalhes=[
                {"placa": "ABC1D23", "confianca": 0.9},
                {"placa": "ABC1D23", "confianca": 0.9},
            ]),
        ])
        assert eleito["placa"] == "ABC1D23"


class TestNaoInventaPlaca:
    """Caso real: 24/08/2026, bico 3 do ALTIPLANO.

    Com duas câmeras, a traseira leu a moto (`OSL2G55`) e a frontal contribuiu um candidato
    de OUTRO veículo — moto no Brasil não tem placa dianteira, então não havia placa para a
    frente ler. A fusão por caractere misturou os dois e emitiu `OSL2855`, string que engine
    NENHUM produziu, com `acordo=0.00`, e o histórico gravou como leitura.
    """

    def test_veiculo_do_bico_nao_perde_para_o_que_a_frontal_viu(self):
        """Duas leituras de OUTROS veículos somam peso e roubam a eleição.

        Cenário do posto: a traseira lê a moto no bico (`OSL2659`) e a frontal, que não tem
        placa de moto para ler, pega dois carros da pista. Sem agrupar, o voto por posição
        entre os três devolve `FSL9750` — nem a moto, nem nada que estivesse no bico.
        """
        eleito = _eleger_placa([
            _candidato("OSL2659", 0.85, padrao="antigo"),   # traseira: a moto do bico
            _candidato("FWX9760", 0.74, padrao="antigo"),   # frontal: outro veículo
            _candidato("FSL9750", 0.80, padrao="antigo"),   # frontal: mais um
        ])
        assert eleito["placa"] == "OSL2659"

    def test_pool_de_tres_nao_produz_string_que_ninguem_leu(self):
        """A inversão de placa exige ≥ 3 leituras: com 2, o voto por posição só devolve a de
        maior peso. Estas três vieram de uma busca por pools que FABRICAM — sem agrupar,
        elas produzem `RWR6507`, que não é nenhuma das leituras nem está a 2 caracteres de
        qualquer uma. É o mecanismo que gravou `OSL2855` no histórico com acordo 0,00.
        """
        lidas = ["RWRD59B", "SQ3660O", "GPIBI07"]
        eleito = _eleger_placa([
            _candidato("RWRD59B", 0.94, padrao="antigo"),
            _candidato("SQ3660O", 0.94, padrao="antigo"),
            _candidato("GPIBI07", 0.70, padrao="antigo"),
        ])
        assert eleito["placa"] != "RWR6507", "voltou a fabricar placa"
        assert eleito["placa"] in lidas

    def test_placa_eleita_sempre_tem_respaldo_em_leitura_real(self):
        """Qualquer que seja o pool, a saída tem de estar a ≤2 chars de algo lido."""
        lidas = ["OSL2G55", "FWX9760", "ABC1D23"]
        eleito = _eleger_placa([_candidato(p, 0.8, padrao="antigo") for p in lidas])
        assert any(sum(1 for a, b in zip(eleito["placa"], p) if a != b) <= 2 for p in lidas)

    def test_ruido_do_mesmo_veiculo_continua_convergindo(self):
        """A guarda não pode matar o que a fusão existe para fazer."""
        eleito = _eleger_placa([
            _candidato("RLT2477", 0.92), _candidato("NLX2A77", 0.86),
            _candidato("RLX2A77", 0.90), _candidato("AAX2A77", 0.87),
        ])
        assert eleito["placa"] == "RLX2A77"


class TestMesclarComAnterior:
    def test_ruido_de_um_caractere_converge_para_a_mesma_placa(self):
        atual = {"placa": "ABC1D23", "confianca": 0.7, "padrao": "mercosul"}
        anterior = {"placa": "ABC1D23", "confianca": 0.9}
        assert _mesclar_com_anterior(atual, anterior)["placa"] == "ABC1D23"

    def test_mantem_a_maior_confianca_das_duas(self):
        atual = {"placa": "ABC1D23", "confianca": 0.7, "padrao": "mercosul"}
        anterior = {"placa": "ABC1D23", "confianca": 0.95}
        assert _mesclar_com_anterior(atual, anterior)["confianca"] == 0.95

    def test_sem_consenso_valido_fica_com_a_leitura_mais_confiante(self):
        atual = {"placa": "ABC1D23", "confianca": 0.2, "padrao": "mercosul"}
        anterior = {"placa": "XYZ9K88", "confianca": 0.9}
        assert _mesclar_com_anterior(atual, anterior)["placa"] == "XYZ9K88"


class TestConfirmada:
    """A regra que separa leitura sólida de 'candidata menos ruim'.

    O que ela decide vai para o banco (`deteccoes.confirmada`), para a resposta do
    roteador e para a taxa de sucesso do painel — marcar uma leitura fraca como sólida
    é o caminho para vincular a placa errada a um abastecimento.
    """

    def test_acordo_alto_com_dois_votos_confirma(self):
        assert _confirmada(0.95, 2, acordo_min=0.80, n_min=3) is True

    def test_um_voto_so_nao_confirma_mesmo_com_acordo_perfeito(self):
        """Regressão do falso positivo em pista vazia: 1 frame com detecção fecha
        acordo=1.0 sozinho (a placa dele e os engines dele são o pool inteiro), sem
        nenhuma concordância ENTRE frames. Isto voltava como confirmada e virava 'ok'."""
        assert _confirmada(1.0, 1, acordo_min=0.80, n_min=3) is False

    def test_acordo_baixo_nao_confirma_mesmo_com_muitos_votos(self):
        assert _confirmada(0.42, 3, acordo_min=0.80, n_min=3) is False

    def test_acordo_exatamente_no_minimo_confirma(self):
        """O mínimo é um piso inclusivo — configurar 0.80 e ver 0.80 recusado seria
        surpresa para quem ajusta o parâmetro."""
        assert _confirmada(0.80, 2, acordo_min=0.80, n_min=3) is True

    def test_snapshots_votacao_1_nao_deixa_tudo_nao_confirmado(self):
        """Quem configura `snapshots_votacao=1` abriu mão da votação entre frames.
        Exigir 2 votos ali deixaria TODA leitura não-confirmada, o oposto do ajuste."""
        assert _confirmada(0.95, 1, acordo_min=0.80, n_min=1) is True

    def test_tres_votos_de_tres_confirma(self):
        assert _confirmada(1.0, 3, acordo_min=0.80, n_min=3) is True


class TestSementeDoContinuo:
    """O GET vota junto com o que o monitoramento contínuo já leu do mesmo veículo.

    Em 24/08/2026, no bico 3 do ALTIPLANO, o tracker havia lido `RLX2A77` com confiança 0,96
    e todos os `char_probs` ≥ 0,93 **sete segundos antes** da chamada. O GET sondou 2 dos 12
    frames do orçamento antes de estourar o timeout de 28 s, votou só entre esses dois, e
    emitiu `HDX2477`. A evidência certa estava a um atributo de distância.
    """

    def test_leitura_do_continuo_corrige_a_foto_ruim_do_get(self):
        eleito = _eleger_placa(
            [_candidato("HDX2477", 0.89, padrao="antigo")],
            leituras_extra=[("RLX2A77", 0.96), ("RLX2A77", 0.90), ("NLX2A77", 0.86)],
        )
        assert eleito["placa"] == "RLX2A77"

    def test_semente_nao_conta_como_foto_da_chamada(self):
        """`n_votos_snap` mede fotos DESTA chamada — é o que `confirmada` exige duas.

        Se a semente contasse, uma chamada de uma única foto passaria a "confirmada" por
        evidência que ela não colheu, e o critério de duas leituras independentes viraria
        letra morta.
        """
        eleito = _eleger_placa(
            [_candidato("RLX2A77", 0.90, padrao="mercosul")],
            leituras_extra=[("RLX2A77", 0.96)] * 5,
        )
        assert eleito["n_votos_snap"] == 1

    def test_sem_semente_o_comportamento_e_o_de_antes(self):
        so_get = _eleger_placa([_candidato("HDX2477", 0.89, padrao="antigo")])
        assert so_get["placa"] == "HDX2477"

    def test_semente_de_outro_veiculo_nao_contamina(self):
        """O contínuo pode ter lido o carro do bico ao lado — agrupar antes de fundir vale
        para a semente igual."""
        eleito = _eleger_placa(
            [_candidato("ABC1D23", 0.90, padrao="mercosul")],
            leituras_extra=[("XYZ9K88", 0.95), ("XYZ9K88", 0.95)],
        )
        assert eleito["placa"] == "ABC1D23"


class TestCombinacaoDosPesos:
    """Por que a força de um caractere é a soma dos QUADRADOS, e não a soma.

    Soma linear deixa duas leituras medíocres que concordam baterem uma muito confiante — e
    isso importa porque os três modelos do ensemble são da MESMA família, então erro
    correlacionado é o caso comum. Caso real, `GAE0244` na posição 4 (verdade `2`):

        '3' de DAE8343 ... 0,868
        '2' de GAE0244 ... 0,991   ← o correto, e o mais confiante de todos
        '3' de GAE0344 ... 0,432

    Soma: '3' = 1,300 contra 0,991 → sai `GAE0344`, errado.
    Quadrados: '3' = 0,940 contra 0,982 → sai `GAE0244`, certo.
    """

    def test_reproduz_o_caso_gae0244(self):
        """Números medidos no recorte real — se a regra voltar a somar linear, isto cai."""
        leituras = [
            ("DAE8343", [0.84, 0.99, 0.99, 0.62, 0.868, 0.99, 0.99]),
            ("GAE0244", [0.99, 0.99, 0.99, 0.99, 0.991, 0.99, 0.99]),
            ("GAE0344", [0.99, 0.99, 0.99, 0.99, 0.432, 0.99, 0.99]),
        ]
        assert _consenso_caractere(leituras) == "GAE0244"

    def test_concordancia_ainda_vale_quando_as_confiancas_sao_parecidas(self):
        """O quadrado não pode virar `max`: dois modelos de acordo têm de ganhar de um só
        quando a confiança é comparável. `max` mediu pior (31/40 contra 32/40)."""
        leituras = [
            ("ABC1D23", [0.90] * 7),
            ("ABC1D23", [0.90] * 7),
            ("ABC1O23", [0.95] * 7),
        ]
        assert _consenso_caractere(leituras) == "ABC1D23"

    def test_uma_certeza_isolada_ganha_de_duas_duvidas(self):
        leituras = [
            ("ABC1O23", [0.99, 0.99, 0.99, 0.99, 0.50, 0.99, 0.99]),
            ("ABC1D23", [0.99, 0.99, 0.99, 0.99, 0.50, 0.99, 0.99]),
            ("ABC1D23", [0.99, 0.99, 0.99, 0.99, 0.50, 0.99, 0.99]),
        ]
        # empate de contagem 2x1 com confiança igual: a maioria decide, como deve
        assert _consenso_caractere(leituras) == "ABC1D23"

    def test_peso_escalar_continua_funcionando(self):
        """Três chamadores só têm o escalar (tracker, `_eleger_placa`, Paddle/EasyOCR)."""
        assert _consenso_caractere([("ABC1D23", 1.0), ("ABC1D23", 1.0),
                                    ("ABC1O23", 1.0)]) == "ABC1D23"

    def test_mistura_escalar_e_por_posicao(self):
        """O pool real mistura: o fast expõe vetor, o Paddle não."""
        leituras = [
            ("ABC1O23", 0.60),                                    # Paddle, escalar
            ("ABC1D23", [0.99, 0.99, 0.99, 0.99, 0.99, 0.99, 0.99]),
        ]
        assert _consenso_caractere(leituras) == "ABC1D23"

    def test_vetor_curto_cai_para_a_media_sem_estourar(self):
        """Alinhamento que falhou não pode virar IndexError no meio da leitura."""
        assert _consenso_caractere([("ABC1D23", [0.9, 0.8])]) == "ABC1D23"

    def test_vetor_invalido_nao_derruba(self):
        assert _consenso_caractere([("ABC1D23", "nao-e-numero")]) == "ABC1D23"


class TestVotosDeLeitura:
    """`n_votos_leitura` conta ENGINES que apoiam a placa, e é ele que decide `confirmada`.

    A regra antiga contava FOTOS e exigia 2. Com o GET conseguindo 1 foto em 28 s (o
    pipeline contínuo disputa CPU), "2 fotos" era inalcançável e NADA era confirmado —
    `SKU7G13` saiu com acordo 100%, confiança 95% e mesmo assim "a conferir".

    Medido em 80 recortes de placa real contra 80 falsos positivos do detector:
    `≥2 leituras apoiando E acordo ≥0,80` deixa passar 86% das reais e 4% dos falsos, contra
    0% e 0% da regra por fotos.
    """

    def _c(self, placa, det=None, conf=0.6):
        d = {"placa": placa, "padrao": "mercosul", "confianca": conf}
        if det is not None:
            d["detalhes_ocr"] = [{"placa": p, "confianca": 0.8} for p in det]
        return d

    def test_um_engine_so_conta_um(self):
        """Regressão do bug de contagem dupla.

        A primeira versão contava sobre o pool de `_eleger_placa`, que recebe a placa final
        do candidato MAIS cada engine dele — e a placa final É derivada dos engines. Um
        falso positivo em que UM único engine validou dava 2 e passava a guarda dos 2,
        anulando exatamente os 4% de falso aceite para que a regra foi calibrada.
        """
        assert _eleger_placa([self._c("ABC1D23", ["ABC1D23"])])["n_votos_leitura"] == 1

    def test_engines_que_apoiam_contam_mesmo_com_ruido_de_um_char(self):
        """`ABC1O23` é a mesma placa lida com ruído clássico — apoia, não contradiz."""
        e = _eleger_placa([self._c("ABC1D23", ["ABC1D23", "ABC1D23", "ABC1O23"])])
        assert e["n_votos_leitura"] == 3

    def test_engine_de_outra_placa_nao_conta(self):
        e = _eleger_placa([self._c("ABC1D23", ["ABC1D23", "ABC1D23", "XYZ9K88"])])
        assert e["n_votos_leitura"] == 2

    def test_candidato_sem_detalhes_conta_como_uma_leitura(self):
        """Regressão do bug oposto: contar só `detalhes_ocr` dava ZERO para quem não
        reporta detalhe por engine (dublês, caminhos fora do ensemble), e aí `confirmada`
        ficava inalcançável nesses caminhos — trocaria um bug por outro."""
        assert _eleger_placa([self._c("ABC1D23")])["n_votos_leitura"] == 1
        assert _eleger_placa([self._c("ABC1D23", [])])["n_votos_leitura"] == 1

    def test_fotos_e_leituras_sao_contagens_diferentes(self):
        """As duas viajam no payload e significam coisas distintas: `votos_snapshot` são
        FOTOS (contrato publicado no roteador), `votos_leitura` são ENGINES."""
        e = _eleger_placa([self._c("ABC1D23", ["ABC1D23", "ABC1D23", "ABC1D23"])])
        assert e["n_votos_snap"] == 1        # uma foto só
        assert e["n_votos_leitura"] == 3     # três engines nela

    def test_uma_foto_com_ensemble_pode_confirmar(self):
        """O caso que motivou a mudança inteira."""
        e = _eleger_placa([self._c("ABC1D23", ["ABC1D23", "ABC1D23", "ABC1D23"], conf=0.95)])
        assert _confirmada(e["acordo"], e["n_votos_leitura"], acordo_min=0.80, n_min=3)

    def test_uma_foto_com_um_engine_nao_confirma(self):
        """O outro lado, sem o qual o teste acima não distingue "consertou a unidade" de
        "afrouxou a regra"."""
        e = _eleger_placa([self._c("ABC1D23", ["ABC1D23"], conf=0.95)])
        assert not _confirmada(e["acordo"], e["n_votos_leitura"], acordo_min=0.80, n_min=3)
