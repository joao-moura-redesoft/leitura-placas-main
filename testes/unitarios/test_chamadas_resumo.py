"""`banco.chamadas_resumo`: as médias de acordo/duração viraram uma query só (CASE no
lugar de dois SELECT separados) — achado C3 do review de 28/08/2026. O número tem que
continuar batendo com o cálculo feito "na mão" sobre as mesmas linhas.
"""
from __future__ import annotations

from app.core import banco


def _chamada(status: str, acordo, duracao_ms):
    banco.registrar_chamada(
        entidade="e", cnpj="x", automacao="1", bico="1", status=status, motivo="",
        acordo=acordo, duracao_ms=duracao_ms,
    )


class TestMediasDeAcordoEDuracao:
    def test_batem_com_o_calculo_separado(self, ambiente):
        _chamada("ok", 0.9, 100)
        _chamada("nao_confirmada", 0.5, 200)
        _chamada("sem_placa", None, 300)   # acordo NULL: fora da média de acordo

        r = banco.chamadas_resumo(horas=24)

        assert r["acordo_medio"] == round((0.9 + 0.5) / 2, 3)
        assert r["duracao_media_ms"] == int((100 + 200 + 300) / 3)
        assert r["total"] == 3

    def test_status_fora_do_filtro_nao_entra_na_media_de_acordo(self, ambiente):
        """`acordo` só conta status 'ok'/'nao_confirmada' — 'erro_camera' fica de fora
        mesmo que tenha um valor de acordo gravado (ex.: dado legado/migrado)."""
        _chamada("ok", 0.8, 50)
        banco.registrar_chamada(entidade="e", cnpj="x", automacao="1", bico="1",
                                status="erro_camera", motivo="sem_camera", acordo=0.99,
                                duracao_ms=None)

        r = banco.chamadas_resumo(horas=24)

        assert r["acordo_medio"] == 0.8
        assert r["duracao_media_ms"] == 50

    def test_sem_chamada_nenhuma_e_none_nao_zero(self, ambiente):
        """Nenhuma chamada no período não é "média zero" — é "sem dado"."""
        r = banco.chamadas_resumo(horas=24)
        assert r["acordo_medio"] is None
        assert r["duracao_media_ms"] is None
        assert r["total"] == 0
