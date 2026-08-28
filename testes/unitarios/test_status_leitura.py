"""A regra que decide o que conta como leitura bem-sucedida.

Esta classificação alimenta a taxa de sucesso do painel e diz ao atendente se pode
confiar na placa — uma leitura fraca contada como 'ok' vira cobrança no cliente errado.
"""
from __future__ import annotations

from app.visao.leitura import PERFIL_COMPLETO, PERFIL_RAPIDO
from app.web.leitura import _status_da_leitura, perfil_pedido


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

    def test_timeout_sem_consenso_nao_conta_como_sucesso(self):
        """Timeout SEM consenso continua rebaixando — é o caso que a regra defende.

        Este teste dizia "timeout nunca é sucesso, nem com `confirmada`", e estava certo
        enquanto a confirmação exigia 2 FOTOS: o laço só parava por `acordo` quando fechava
        consenso, então timeout significava mesmo "nunca fechou".

        Deixou de valer em 25/08/2026, quando `confirmada` passou a contar LEITURAS (o
        ensemble dá 3-4 por foto) e o GET, conseguindo 1 foto em 28 s, passou a estourar o
        tempo DEPOIS de já ter decidido. Ver o teste seguinte.
        """
        # `confirmada: None` (consenso DESCONHECIDO), e não `False`: com `False` quem
        # responde é o gate anterior, com "consenso insuficiente". O ramo do timeout só é
        # alcançável quando o consenso é desconhecido — chamada antiga, ou origem que não
        # passa pelo laço. Escrever `False` aqui testaria um estado inalcançável e passaria
        # medindo outra coisa.
        status, motivo = _status_da_leitura(
            {"placa": "RHO1J15", "confirmada": None, "acordo": 0.40,
             "parada_motivo": "timeout"})
        assert status == "nao_confirmada"
        assert "tempo esgotado" in motivo

    def test_confirmada_falsa_responde_antes_do_timeout(self):
        """Os dois gates coexistem, e a ordem importa para a MENSAGEM.

        Sem consenso, o motivo tem de dizer que faltou consenso — e relatar os números —,
        não culpar o relógio. A causa é a mesma, mas quem lê o histórico precisa saber se
        faltou evidência ou faltou tempo.
        """
        status, motivo = _status_da_leitura(
            {"placa": "RHO1J15", "confirmada": False, "acordo": 0.40,
             "parada_motivo": "timeout"})
        assert status == "nao_confirmada"
        assert "consenso insuficiente" in motivo

    def test_timeout_com_consenso_fechado_e_sucesso(self):
        """Caso real `SKU7G13`: acordo 100%, confiança 95%, 4 leituras concordantes — e
        rebaixado por ter esgotado o tempo depois de já ter decidido.

        Rebaixar aqui não protege ninguém: esconde leitura boa atrás de "a conferir" e, com
        `apiplacas_exigir_confirmada` ligado, impede a consulta de dados do veículo para
        sempre. Timeout passou a significar "não sobrou tempo para MAIS fotos", que é
        diferente de "não fechou consenso".
        """
        status, motivo = _status_da_leitura(
            {"placa": "SKU7G13", "confirmada": True, "acordo": 1.0,
             "parada_motivo": "timeout"})
        assert status == "ok"
        assert motivo == ""

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


class TestPerfilPedido:
    """A regra que decide quanto a chamada vai durar e quanta acuracia abre mao."""

    def test_sem_o_parametro_e_o_perfil_completo(self):
        assert perfil_pedido(False, {"rapido_ativo": "sim"}) == (PERFIL_COMPLETO, "")

    def test_com_o_parametro_e_o_perfil_rapido(self):
        assert perfil_pedido(True, {"rapido_ativo": "sim"}) == (PERFIL_RAPIDO, "")

    def test_desligado_no_servidor_roda_completo_e_avisa(self):
        """`rapido_ativo=nao` nao e erro para quem chamou — e o interruptor para desligar
        o perfil leve num posto onde ele se mostrou ruim, sem mexer no roteador. Mas o
        chamador precisa saber, senao espera em 5s uma resposta que leva 30."""
        perfil, aviso = perfil_pedido(True, {"rapido_ativo": "nao"})
        assert perfil == PERFIL_COMPLETO
        assert "desativado" in aviso

    def test_desligado_nao_avisa_quem_nao_pediu(self):
        """Aviso so para quem fez a pergunta: poluir o `avisos` de toda chamada completa
        faria o painel agrupar um nao-evento como se fosse problema de infraestrutura."""
        assert perfil_pedido(False, {"rapido_ativo": "nao"}) == (PERFIL_COMPLETO, "")

    def test_config_sem_a_chave_mantem_o_modo_disponivel(self):
        """Instalacao antiga, sem `rapido_ativo` no config.txt: o default de
        `config.PADROES` e 'sim', e quem pedir o modo tem de recebe-lo."""
        assert perfil_pedido(True, {})[0] == PERFIL_RAPIDO
