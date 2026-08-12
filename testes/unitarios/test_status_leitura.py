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
