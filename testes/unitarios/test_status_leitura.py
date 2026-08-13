"""A regra que decide o que conta como leitura bem-sucedida.

Esta classificação alimenta a taxa de sucesso do painel e diz ao atendente se pode
confiar na placa — uma leitura fraca contada como 'ok' vira cobrança no cliente errado.
"""
from __future__ import annotations

from app.web.leitura import _status_da_leitura


class TestStatusDaLeitura:
    def test_leitura_com_consenso_e_sucesso(self):
        status, motivo = _status_da_leitura(
            {"placa": "ABC1D23", "confirmada": True, "acordo": 0.95})
        assert status == "ok"
        assert motivo == ""

    def test_leitura_sem_consenso_nao_conta_como_sucesso(self):
        status, motivo = _status_da_leitura(
            {"placa": "HPY2371", "confirmada": False, "acordo": 0.42,
             "parada_motivo": "timeout"})
        assert status == "nao_confirmada"
        assert "0.42" in motivo and "timeout" in motivo

    def test_motivo_nao_culpa_o_acordo_quando_o_que_faltou_foi_voto(self):
        """`confirmada` cai por acordo baixo OU por votos de menos. Dizer sempre
        "acordo abaixo do mínimo" produzia a frase absurda "acordo 1.00 abaixo do
        mínimo" — e mandava quem investiga mexer no parâmetro errado."""
        status, motivo = _status_da_leitura(
            {"placa": "RHO1J15", "confirmada": False, "acordo": 1.0,
             "votos_snapshot": 1, "total_snapshots": 3, "parada_motivo": "timeout"})
        assert status == "nao_confirmada"
        assert "abaixo do mínimo" not in motivo
        assert "1/3 fotos" in motivo

    def test_motivo_sem_contagem_de_fotos_nao_inventa_numero(self):
        """Origens que não passam pelo loop não têm votos_snapshot."""
        _, motivo = _status_da_leitura(
            {"placa": "HPY2371", "confirmada": False, "acordo": 0.42})
        assert "fotos" not in motivo and "0.42" in motivo

    def test_sem_placa_continua_sem_placa(self):
        status, motivo = _status_da_leitura(
            {"placa": None, "mensagem": "Nenhuma placa detectada nos frames"})
        assert status == "sem_placa"
        assert motivo == "Nenhuma placa detectada nos frames"

    def test_consenso_desconhecido_nao_rebaixa(self):
        """`confirmada` ausente = origem que não passa pelo loop, não leitura fraca."""
        assert _status_da_leitura({"placa": "ABC1D23"})[0] == "ok"

    def test_acordo_ausente_nao_quebra_a_formatacao(self):
        """Regressão: formatar None com :.2f levantaria TypeError e derrubaria a
        classificação de uma leitura que já era problemática."""
        status, motivo = _status_da_leitura(
            {"placa": "ABC1D23", "confirmada": False, "acordo": None})
        assert status == "nao_confirmada"
        assert "?" in motivo

    def test_timeout_nao_conta_como_sucesso_mesmo_confirmada(self):
        """Sair por timeout = o loop nunca fechou consenso (o outro motivo seria
        'acordo'). Contar como 'ok' inflava a taxa de sucesso do painel justamente
        com as leituras que precisam de conferência antes de virar cobrança.
        """
        status, motivo = _status_da_leitura(
            {"placa": "RHO1J15", "confirmada": True, "acordo": 1.0,
             "parada_motivo": "timeout"})
        assert status == "nao_confirmada"
        assert "tempo esgotado" in motivo

    def test_parada_por_acordo_continua_sucesso(self):
        status, motivo = _status_da_leitura(
            {"placa": "ABC1D23", "confirmada": True, "acordo": 0.93,
             "parada_motivo": "acordo"})
        assert status == "ok"
        assert motivo == ""

    def test_parada_por_max_tentativas_com_consenso_continua_sucesso(self):
        """Esgotar as tentativas não é o mesmo que esgotar o tempo: aqui o loop
        analisou tudo que se propôs a analisar e o consenso fechou."""
        assert _status_da_leitura(
            {"placa": "ABC1D23", "confirmada": True, "acordo": 0.9,
             "parada_motivo": "max_tentativas"})[0] == "ok"
