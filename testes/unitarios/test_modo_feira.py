"""Modo feira (mock de demonstração) — app/visao/feira.py e o gancho em `ler_placa`.

O que estes testes protegem, em ordem de importância:

1. DESLIGADO NÃO MUDA NADA. É a garantia que permite o recurso existir num servidor de
   produção: quem só atualizou o código não pode ganhar comportamento novo.
2. A placa do VISITANTE não é sequestrada. O modo existe para o carrinho de demonstração;
   se ele capturasse a placa que um cliente testa do celular, destruiria a própria demo.
3. A leitura mockada é RASTREÁVEL. Sai marcada em `avisos` e gravada com `origem="feira"`,
   fora do filtro 'producao'. Leitura mockada é dado sintético, e dado sintético já
   inverteu o sinal de uma medição neste projeto — se ela entrar na taxa de acerto, o
   número passa a medir o instrumento.
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from app.core import banco
from app.core import config
from app.visao import feira
from app.visao import leitura as leitura_mod
from app.web import cadastro as cad


class _RequisicaoFalsa:
    """Request mínima para as rotas chamadas DIRETO (sem subir o app).

    `deps.quem_pede`/`usuario_atual` só leem `request.state.user`; ausente significa
    "veio pela api_key global", que a auditoria registra como tal. É o bastante para
    exercitar o corpo da rota sem TestClient.
    """

    class _Estado:
        pass

    def __init__(self):
        self.state = self._Estado()

from test_payload_leitura import (  # noqa: F401  (fixtures vêm por nome)
    CFG, _DetectorFalso, _OcrFalso, _ler, visao_falsa,
)

DEMO = "MOK3H92"
EMPRESA_DEMO = 42          # o posto de demonstração
EMPRESA_REAL = 7           # um cliente de verdade na mesma instalação
CFG_FEIRA = {**CFG, "feira_ativo": "sim", "feira_placas": "MOK-3H92",
             "feira_tolerancia": "2", "feira_empresa_id": str(EMPRESA_DEMO)}


class TestCasar:
    """`casar` é pura — testável sem câmera, sem rede e sem banco, como `_pode_gastar`."""

    @pytest.mark.parametrize("lida, esperado", [
        ("MOK3H92", DEMO),      # exata
        ("mok-3h92", DEMO),     # hífen e minúsculas
        ("M0K3H9Z", DEMO),      # 2 trocas — o caso real da mini-placa
        ("MOK3H9", DEMO),       # OCR perdeu um caractere
        # Leitura parcial do carrinho: 2 caracteres faltando = distância 2, casa. É o
        # desejado numa demo, e a própria distância é o piso — "MOK3" já fica a 3 e não
        # casa, então não há como uma string curta qualquer virar a placa de demonstração.
        ("MOK3H", DEMO),
        ("MOK3", None),
        ("ABC1D23", None),      # placa de visitante
        ("QFE9E22", None),      # outra placa real
        ("", None),
        (None, None),
    ])
    def test_casamento(self, lida, esperado):
        got = feira.casar(lida, CFG_FEIRA, EMPRESA_DEMO)
        if esperado is None:
            assert got is None, f"{lida!r} NÃO devia casar, casou com {got!r}"
        else:
            assert got == esperado

    def test_desligado_nunca_casa(self):
        assert feira.casar(DEMO, {**CFG_FEIRA, "feira_ativo": "nao"}, EMPRESA_DEMO) is None

    def test_lista_vazia_nunca_casa(self):
        """`feira_ativo=sim` sem placa cadastrada é um modo que não pode fazer nada —
        e `ativo()` diz isso, para a faixa na tela não mentir que a demo está armada."""
        cfg = {**CFG_FEIRA, "feira_placas": ""}
        assert not feira.ativo(cfg)
        assert feira.casar(DEMO, cfg, EMPRESA_DEMO) is None

    def test_empate_nao_casa(self):
        """Duas placas de demo à mesma distância: desiste em vez de chutar.

        Chutar produziria o pior resultado possível numa feira — o carrinho A exibindo
        a placa do carrinho B, com confiança 1.0.
        """
        cfg = {**CFG_FEIRA, "feira_placas": "MOK3H92,MOK3H93"}
        assert feira.casar("MOK3H9X", cfg, EMPRESA_DEMO) is None

    def test_tolerancia_zero_exige_exata(self):
        cfg = {**CFG_FEIRA, "feira_tolerancia": "0"}
        assert feira.casar("MOK3H92", cfg, EMPRESA_DEMO) == DEMO
        assert feira.casar("M0K3H92", cfg, EMPRESA_DEMO) is None

    def test_tolerancia_invalida_cai_no_padrao(self):
        """Config digitada à mão não pode derrubar a leitura."""
        assert feira.casar("M0K3H9Z", {**CFG_FEIRA, "feira_tolerancia": "abc"},
                          EMPRESA_DEMO) == DEMO


class TestGanchoNaLeitura:
    def test_desligado_nao_muda_nada(self, ambiente, visao_falsa):
        """A garantia que deixa o recurso existir num servidor de produção."""
        visao_falsa(_DetectorFalso(), _OcrFalso("MOK3H93"))
        base = _ler(cfg=CFG)
        comigo = _ler(cfg={**CFG, "feira_ativo": "nao", "feira_placas": DEMO,
                           "feira_empresa_id": str(EMPRESA_DEMO)},
                      empresa_id=EMPRESA_DEMO)
        assert base["placa"] == comigo["placa"] == "MOK3H93"
        assert not [a for a in comigo["avisos"] if "feira" in a.lower()]

    def test_mock_prevalece_sobre_leitura_confiante(self, ambiente, visao_falsa):
        """O OCR leu MOK3H93 com confiança alta; o cadastro diz MOK3H92. Vence o cadastro.

        É o requisito "mockado prevalece": o snap roda DEPOIS da eleição e da fusão, então
        sobrepõe até uma leitura real que errou só um caractere.
        """
        visao_falsa(_DetectorFalso(), _OcrFalso("MOK3H93"))
        r = _ler(cfg=CFG_FEIRA, empresa_id=EMPRESA_DEMO)
        assert r["placa"] == DEMO
        assert r["confianca"] == 1.0
        assert r["acordo"] == 1.0
        assert r["confirmada"] is True

    def test_leitura_mockada_se_declara(self, ambiente, visao_falsa):
        """Sem isto o mock seria indistinguível de leitura real — o que o torna perigoso."""
        visao_falsa(_DetectorFalso(), _OcrFalso("MOK3H93"))
        r = _ler(cfg=CFG_FEIRA, empresa_id=EMPRESA_DEMO)
        avisos = " ".join(r["avisos"]).lower()
        assert "feira" in avisos and "mock" in avisos

    def test_placa_de_visitante_passa_intacta(self, ambiente, visao_falsa):
        """O cliente testando a placa do celular continua no caminho real."""
        visao_falsa(_DetectorFalso(), _OcrFalso("ABC1D23"))
        r = _ler(cfg=CFG_FEIRA, empresa_id=EMPRESA_DEMO)
        assert r["placa"] == "ABC1D23"
        assert not [a for a in r["avisos"] if "feira" in a.lower()]

    def test_sem_placa_nenhuma_nao_inventa(self, ambiente, visao_falsa):
        """Sem string do OCR não há o que casar — o modo não pode fabricar leitura do nada.

        É o buraco conhecido do snap (mini-placa pequena demais) e a razão de
        `feira_marcadores` existir como fase 2.
        """
        visao_falsa(_DetectorFalso(), _OcrFalso(None))
        r = _ler(cfg=CFG_FEIRA, empresa_id=EMPRESA_DEMO)
        assert r["placa"] is None

    def test_gravada_fora_da_producao(self, ambiente, visao_falsa):
        """`origem='feira'` some do filtro 'producao' e aparece no filtro próprio.

        Se ela contasse como produção, a taxa de acerto do painel passaria a medir o
        mock em vez do sistema.
        """
        visao_falsa(_DetectorFalso(), _OcrFalso("MOK3H93"))
        _ler(cfg=CFG_FEIRA, empresa_id=EMPRESA_DEMO, origem="roteador")
        assert [d["placa"] for d in banco.listar_deteccoes(origem="feira")] == [DEMO]
        assert banco.listar_deteccoes(origem="producao") == []


class TestEscopoPorPosto:
    """O mock só pode agir no POSTO DE DEMONSTRAÇÃO.

    Esta classe é a mais importante do arquivo. Sem ela, `feira_ativo=sim` valeria para a
    instalação inteira, e um servidor que atende clientes reais passaria a devolver placa
    mockada para eles — placa que não veio do OCR, com `confirmada: true`, e que o roteador
    usa para COBRAR. O escopo é o que permite o posto de demonstração conviver com os reais.
    """

    def test_posto_real_nunca_e_mockado(self):
        """Mesma placa, mesmo config — muda só o posto, e o mock não age."""
        assert feira.casar(DEMO, CFG_FEIRA, EMPRESA_DEMO) == DEMO
        assert feira.casar(DEMO, CFG_FEIRA, EMPRESA_REAL) is None

    def test_sem_posto_de_demo_o_mock_fica_desarmado(self):
        """`feira_ativo=sim` sozinho não pode mockar nada — falha FECHADA.

        É o cenário de alguém ligar o interruptor num servidor de produção sem ter criado
        o posto de demonstração. Antes do escopo isso mockava todo mundo.
        """
        cfg = {**CFG_FEIRA, "feira_empresa_id": ""}
        assert not feira.ativo(cfg)
        for empresa in (EMPRESA_DEMO, EMPRESA_REAL, None):
            assert feira.casar(DEMO, cfg, empresa) is None

    def test_empresa_ausente_nao_e_curinga(self):
        """Chamador que esqueceu de passar o posto não ganha mock — falha fechada.

        Se `None` casasse com qualquer coisa, bastaria um caminho novo de leitura esquecer
        o parâmetro para o escopo inteiro virar decoração.
        """
        assert feira.casar(DEMO, CFG_FEIRA, None) is None

    def test_feira_empresa_id_ilegivel_desarma(self):
        """Config editada à mão não pode virar escopo que casa por acidente."""
        cfg = {**CFG_FEIRA, "feira_empresa_id": "posto-da-feira"}
        assert feira.empresa_demo(cfg) is None
        assert not feira.ativo(cfg)
        assert feira.casar(DEMO, cfg, EMPRESA_DEMO) is None

    def test_gancho_respeita_o_escopo(self, ambiente, visao_falsa):
        """O mesmo pelo caminho real de `ler_placa`, não só na função pura."""
        visao_falsa(_DetectorFalso(), _OcrFalso("MOK3H93"))
        demo = _ler(cfg=CFG_FEIRA, empresa_id=EMPRESA_DEMO)
        real = _ler(cfg=CFG_FEIRA, empresa_id=EMPRESA_REAL)
        assert demo["placa"] == DEMO
        assert real["placa"] == "MOK3H93"
        assert not [a for a in real["avisos"] if "feira" in a.lower()]


class TestPostoDeDemonstracao:
    """`POST/DELETE /api/feira/posto` — monta e desmonta a árvore inteira do estande.

    O que estes testes protegem:

    - **Idempotência.** `empresas.cnpj` é UNIQUE global; sem reaproveitar, o segundo clique
      no botão devolveria 409 bem na hora de montar a demonstração.
    - **A árvore fica utilizável.** Câmera com o `empresa_id` do posto (senão
      `_validar_camera_do_posto` recusaria o bico), automação e bico criados, ROI
      preenchida — o posto tem que aparecer como `pronto` em /postos, não como pendência.
    - **O mock é armado e desarmado junto.** Criar preenche `feira_empresa_id`; remover
      limpa. É esse campo que separa "mock desarmado" de "mock mirando um posto real".
    """

    def _criar(self, monkeypatch, frame=None):
        """Chama o endpoint com a captura de câmera dublada.

        A captura real abriria uma webcam/RTSP, que não existe na suíte — e o que este
        teste mede é o CADASTRO, não a câmera.
        """
        import app.visao.camera as camera_mod
        import app.web.cadastro as cad
        monkeypatch.setattr(camera_mod, "capturar_frame_unico",
                            lambda **kw: frame, raising=True)
        # A subida do pipeline vira no-op: ela roda em thread de fundo e abriria conexão.
        import app.web.api as api_rotas
        monkeypatch.setattr(api_rotas, "_iniciar_camera_bg", lambda *a, **k: None)
        return cad.feira_criar_posto({"camera_tipo": "usb", "camera_indice": "0"},
                                     _RequisicaoFalsa())

    def test_cria_a_arvore_inteira(self, ambiente, monkeypatch):
        import numpy as np
        r = self._criar(monkeypatch, frame=np.zeros((480, 640, 3), dtype=np.uint8))

        assert banco.empresas_obter_por_cnpj(cad.FEIRA_CNPJ) is not None
        bico = banco.bicos_obter(r["bico_id"])
        assert bico is not None and bico["camera_id"] == r["camera_id"]
        # A câmera TEM de pertencer ao posto, senão o bico seria inválido para a leitura.
        assert banco.cameras_obter(r["camera_id"])["empresa_id"] == r["empresa_id"]
        # ROI preenchida com as dimensões REAIS do frame capturado.
        assert json.loads(bico["roi"]) == {"x": 0, "y": 0, "w": 640, "h": 480}
        assert not r["aviso"]

    def test_posto_aparece_pronto(self, ambiente, monkeypatch):
        """Sem ROI o posto apareceria como pendência — parece quebrado no estande."""
        import numpy as np
        r = self._criar(monkeypatch, frame=np.zeros((480, 640, 3), dtype=np.uint8))
        posto = next(p for p in cad.postos_listar(_RequisicaoFalsa())
                     if p["id"] == r["empresa_id"])
        assert posto["pronto"] is True
        assert posto["n_bicos_sem_roi"] == 0

    def test_idempotente(self, ambiente, monkeypatch):
        """Dois cliques no botão não podem virar 409 nem duplicar cadastro."""
        import numpy as np
        f = np.zeros((480, 640, 3), dtype=np.uint8)
        um = self._criar(monkeypatch, frame=f)
        dois = self._criar(monkeypatch, frame=f)
        assert um["empresa_id"] == dois["empresa_id"]
        assert um["bico_id"] == dois["bico_id"]
        assert um["camera_id"] == dois["camera_id"]
        assert len(banco.empresas_listar()) == 1
        assert len([e for e in banco.entidades_listar()
                    if e["nome"] == cad.FEIRA_ENTIDADE]) == 1

    def test_camera_muda_nao_camera_nova(self, ambiente, monkeypatch):
        """Recriar apontando para outra câmera REUSA a linha em vez de acumular."""
        import numpy as np
        import app.visao.camera as camera_mod
        import app.web.api as api_rotas
        f = np.zeros((480, 640, 3), dtype=np.uint8)
        um = self._criar(monkeypatch, frame=f)
        monkeypatch.setattr(camera_mod, "capturar_frame_unico", lambda **kw: f)
        monkeypatch.setattr(api_rotas, "_iniciar_camera_bg", lambda *a, **k: None)
        dois = cad.feira_criar_posto(
            {"camera_tipo": "rtsp", "intelbras_host": "10.0.0.9"}, _RequisicaoFalsa())
        assert dois["camera_id"] == um["camera_id"]
        cam = banco.cameras_obter(dois["camera_id"])
        assert cam["camera_tipo"] == "rtsp" and cam["intelbras_host"] == "10.0.0.9"

    def test_arma_e_desarma_o_mock(self, ambiente, monkeypatch):
        import numpy as np
        r = self._criar(monkeypatch, frame=np.zeros((480, 640, 3), dtype=np.uint8))
        assert config.carregar()["feira_empresa_id"] == str(r["empresa_id"])

        import app.visao.pipeline as pipeline_mod
        monkeypatch.setattr(pipeline_mod, "parar_camera", lambda *a, **k: True)
        cad.feira_remover_posto(_RequisicaoFalsa())
        assert config.carregar()["feira_empresa_id"] == ""
        assert banco.empresas_obter_por_cnpj(cad.FEIRA_CNPJ) is None

    def test_camera_muda_falha_nao_derruba_o_cadastro(self, ambiente, monkeypatch):
        """Câmera que não responde vira AVISO, não erro.

        Um cabo solto não pode obrigar a refazer entidade+posto+automação+bico.
        """
        r = self._criar(monkeypatch, frame=None)
        assert banco.bicos_obter(r["bico_id"]) is not None
        assert "câmera não respondeu" in r["aviso"]
        # Sem ROI o bico ainda lê (usa o quadro inteiro), mas fica listado como pendência.
        assert banco.bicos_obter(r["bico_id"])["roi"] is None

    def test_remover_sem_posto_e_404(self, ambiente):
        with pytest.raises(HTTPException) as e:
            cad.feira_remover_posto(_RequisicaoFalsa())
        assert e.value.status_code == 404

    def test_cnpj_da_demo_passa_no_digito_verificador(self):
        """Se falhar, `empresas_inserir` seria recusado por `POST /api/empresas` e o
        posto de demonstração só existiria por caminho interno — divergindo do cadastro
        que um humano consegue criar pela tela."""
        assert cad._cnpj_valido(cad.FEIRA_CNPJ)

    def test_estado_reflete_criado_e_armado(self, ambiente, monkeypatch):
        import numpy as np
        assert cad.feira_estado_posto() == {"existe": False, "armado": False}
        self._criar(monkeypatch, frame=np.zeros((480, 640, 3), dtype=np.uint8))
        est = cad.feira_estado_posto()
        # Existe, mas o interruptor ainda está desligado: os dois estados são distintos e
        # a tela precisa dizer qual é qual.
        assert est["existe"] is True and est["armado"] is False


class TestApontarPostoExistente:
    """`PUT /api/feira/posto` — armar o mock num posto que JÁ EXISTE.

    Existe por causa de uma falha em campo (03/09/2026). O operador tinha o posto
    montado (câmera, automação, bico, área desenhada), ligou `feira_ativo`, cadastrou
    `MOK3H92,DDR1989` — e a leitura devolveu `DDR1887` sem mockar. O casamento estava
    certo: `_distancia('DDR1887','DDR1989')` é 2, dentro da tolerância. O que faltava era
    ARMAMENTO — `feira_empresa_id` vazio, e o escopo é fail-closed.

    Antes disto a única forma de armar era `POST /feira/posto`, que monta um posto NOVO.
    Quem já tinha cadastro real ficava obrigado a montar um segundo posto só para
    demonstrar, e o mock ficava inalcançável onde ele de fato queria usar.
    """

    def _posto(self, nome="Posto Real", cnpj="11222333000181"):
        ent = banco.entidades_inserir({"nome": "REDE"})
        return banco.empresas_inserir({"entidade_id": ent, "cnpj": cnpj, "nome": nome})

    def test_aponta_e_arma(self, ambiente):
        emp = self._posto()
        r = cad.feira_apontar_posto({"empresa_id": emp}, _RequisicaoFalsa())
        assert r["armado"] is True and r["empresa_id"] == emp
        assert config.carregar()["feira_empresa_id"] == str(emp)

    def test_o_mock_passa_a_agir_naquele_posto(self, ambiente):
        """O caso de campo, ponta a ponta: a leitura que saía crua passa a casar."""
        emp = self._posto()
        cad.feira_apontar_posto({"empresa_id": emp}, _RequisicaoFalsa())
        cfg = {**config.carregar(), "feira_ativo": "sim",
               "feira_placas": "MOK3H92,DDR1989", "feira_tolerancia": "2"}
        assert feira.casar("DDR1887", cfg, emp) == "DDR1989"

    def test_outros_postos_continuam_reais(self, ambiente):
        """Apontar para um posto não pode contaminar os vizinhos."""
        alvo = self._posto("Alvo", "11222333000181")
        outro = self._posto("Outro", "11444777000161")
        cad.feira_apontar_posto({"empresa_id": alvo}, _RequisicaoFalsa())
        cfg = {**config.carregar(), "feira_ativo": "sim",
               "feira_placas": "DDR1989", "feira_tolerancia": "2"}
        assert feira.casar("DDR1887", cfg, alvo) == "DDR1989"
        assert feira.casar("DDR1887", cfg, outro) is None

    def test_nulo_desarma_sem_apagar_nada(self, ambiente):
        """Desarmar e APAGAR são ações diferentes: parar de mockar não pode exigir
        destruir o cadastro."""
        emp = self._posto()
        cad.feira_apontar_posto({"empresa_id": emp}, _RequisicaoFalsa())
        r = cad.feira_apontar_posto({"empresa_id": None}, _RequisicaoFalsa())
        assert r["armado"] is False
        assert config.carregar()["feira_empresa_id"] == ""
        assert banco.empresas_obter(emp) is not None      # o posto continua lá

    def test_posto_inexistente_e_404(self, ambiente):
        with pytest.raises(HTTPException) as e:
            cad.feira_apontar_posto({"empresa_id": 9999}, _RequisicaoFalsa())
        assert e.value.status_code == 404

    def test_trocar_de_posto_move_o_escopo(self, ambiente):
        """Apontar para B tem de DESARMAR A — senão dois postos ficariam mockados."""
        a = self._posto("A", "11222333000181")
        b = self._posto("B", "11444777000161")
        cad.feira_apontar_posto({"empresa_id": a}, _RequisicaoFalsa())
        cad.feira_apontar_posto({"empresa_id": b}, _RequisicaoFalsa())
        cfg = {**config.carregar(), "feira_ativo": "sim", "feira_placas": "DDR1989"}
        assert feira.casar("DDR1989", cfg, b) == "DDR1989"
        assert feira.casar("DDR1989", cfg, a) is None
