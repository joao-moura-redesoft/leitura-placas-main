"""`_mesclar_com_historico` — a regra que decide se uma leitura é o MESMO veículo da
anterior (e portanto atualiza aquela linha) ou um evento novo.

Vale teste próprio porque a regra APAGA linha do histórico no caminho da absorção do
pipeline, e porque ela agora trata o teste manual da interface de forma diferente do
abastecimento real — duas decisões que só aparecem em auditoria, tarde demais.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core import banco
from app.visao.leitura import _mesclar_com_historico

BICO, CAMERA = 1, 7


def _envelhecer(det_id: int, segundos: float) -> None:
    """Joga uma detecção para trás no tempo.

    Não dá para testar o cooldown passando `cooldown_seg=0`: no Windows o relógio tem
    granularidade de ~15ms, então o `criado_em` da linha e o `desde` calculado logo
    depois caem no MESMO microssegundo e o `>=` casa — o teste passaria por acidente.
    """
    quando = (datetime.now(timezone.utc) - timedelta(seconds=segundos)).isoformat()
    with banco.cursor() as c:
        c.execute("UPDATE deteccoes SET criado_em=? WHERE id=?", (quando, det_id))


def _leitura(placa="ABC1D23", confianca=0.9):
    return {"placa": placa, "padrao": "mercosul", "confianca": confianca}


def _mesclar(placa="ABC1D23", origem="roteador", cooldown=120.0):
    return _mesclar_com_historico(_leitura(placa), bico_id=BICO, camera_id=CAMERA,
                                  origem=origem, cooldown_seg=cooldown)


class TestLeituraDoRoteador:
    def test_placa_parecida_no_mesmo_bico_atualiza_a_linha_anterior(self, ambiente):
        anterior = banco.registrar_deteccao("ABC1D23", "mercosul", 0.8, bico_id=BICO,
                                            origem="roteador", camera_db_id=CAMERA)
        _, anterior_id = _mesclar("ABC1O23")   # 1 caractere de ruído de OCR
        assert anterior_id == anterior

    def test_placa_diferente_e_evento_novo(self, ambiente):
        banco.registrar_deteccao("ABC1D23", "mercosul", 0.8, bico_id=BICO,
                                 origem="roteador", camera_db_id=CAMERA)
        assert _mesclar("XYZ9K88")[1] is None

    def test_fora_do_cooldown_e_evento_novo(self, ambiente):
        """Mesma placa, mesmo bico, mas horas depois: é outro abastecimento."""
        anterior = banco.registrar_deteccao("ABC1D23", "mercosul", 0.8, bico_id=BICO,
                                            origem="roteador", camera_db_id=CAMERA)
        _envelhecer(anterior, 600)
        assert _mesclar("ABC1D23", cooldown=120.0)[1] is None

    def test_absorve_e_apaga_a_deteccao_do_pipeline_da_mesma_camera(self, ambiente):
        """O mesmo carro costuma cair nas duas origens; a reativa é a que tem
        significado de negócio, então some com a linha do contínuo."""
        pipe = banco.registrar_deteccao("ABC1D23", "mercosul", 0.7, origem="pipeline",
                                        camera_db_id=CAMERA)
        melhor, anterior_id = _mesclar("ABC1D23")
        assert anterior_id is None                      # insere linha nova...
        assert banco.listar_deteccoes(origem="todas") == []   # ...e a do pipeline sumiu
        assert melhor["confianca"] == 0.9
        assert banco.remover_deteccao(pipe) is False


class TestTesteManualNaoMescla:
    """Regressão: o botão "Ler placa" da tela de ROI passava pela mesma regra."""

    def test_cada_aperto_do_botao_vira_uma_linha(self, ambiente):
        """Quem ajusta enquadramento aperta várias vezes de propósito e precisa
        comparar as tentativas — mesclar apagaria justamente essa comparação."""
        banco.registrar_deteccao("ABC1D23", "mercosul", 0.8, bico_id=BICO,
                                 origem="teste", camera_db_id=CAMERA)
        assert _mesclar("ABC1D23", origem="teste")[1] is None

    def test_nao_apaga_a_deteccao_real_do_pipeline(self, ambiente):
        """O pior caso do comportamento antigo: conferir a câmera pela interface
        deletava do histórico uma detecção real do monitoramento contínuo."""
        pipe = banco.registrar_deteccao("ABC1D23", "mercosul", 0.7, origem="pipeline",
                                        camera_db_id=CAMERA)
        _mesclar("ABC1D23", origem="teste")
        assert [d["id"] for d in banco.listar_deteccoes(origem="todas")] == [pipe]

    def test_nao_absorve_leitura_do_roteador(self, ambiente):
        """Nem por mesclagem no mesmo bico: um teste não pode reescrever a placa de
        um abastecimento que já foi cobrado."""
        real = banco.registrar_deteccao("ABC1D23", "mercosul", 0.8, bico_id=BICO,
                                        origem="roteador", camera_db_id=CAMERA)
        assert _mesclar("ABC1O23", origem="teste")[1] is None
        assert banco.listar_deteccoes()[0]["id"] == real
