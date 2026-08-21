"""Regressão: o servidor DNS embutido faz bind em 0.0.0.0:53 e encaminhava resposta
para QUALQUER endereço de origem, sem checar se vinha de rede privada/confiável —
desenho clássico de open resolver, explorável para amplificação/reflexão DNS caso a
porta vaze para a internet por erro de firewall. `_origem_confiavel` é o portão que
passou a existir antes de processar/responder qualquer pacote em `_loop`.
"""
from __future__ import annotations

from app.operacao.dns_server import _origem_confiavel


class TestOrigemConfiavel:
    def test_rede_privada_rfc1918_e_confiavel(self):
        assert _origem_confiavel("10.0.0.5") is True
        assert _origem_confiavel("172.16.4.9") is True
        assert _origem_confiavel("192.168.1.50") is True

    def test_loopback_e_link_local_sao_confiaveis(self):
        assert _origem_confiavel("127.0.0.1") is True
        assert _origem_confiavel("169.254.1.1") is True

    def test_ip_publico_nao_e_confiavel(self):
        assert _origem_confiavel("8.8.8.8") is False
        assert _origem_confiavel("1.1.1.1") is False
        assert _origem_confiavel("93.184.216.34") is False

    def test_entrada_invalida_nao_e_confiavel_nem_levanta(self):
        assert _origem_confiavel("") is False
        assert _origem_confiavel("nao-e-um-ip") is False
