"""`WorkerSupervisor._revisar_hls` não pode consultar `banco.cameras_listar()` a cada
5s indefinidamente — cadastro/streaming_modo mudam por ação deliberada do admin, não
precisam da mesma responsividade da checagem de saúde de pipeline. Achado do review de
28/08/2026.
"""
from __future__ import annotations

import app.operacao.supervisor as sup_mod
from app.operacao.supervisor import WorkerSupervisor, _INTERVALO_HLS_SEG


class _HlsManagerFalso:
    def __init__(self):
        self.chamadas_revisar = 0
        self._ativo = True

    def ativo(self) -> bool:
        return self._ativo

    def revisar(self, cameras) -> None:
        self.chamadas_revisar += 1


def _preparar(monkeypatch):
    hls_falso = _HlsManagerFalso()
    import app.streaming.hls_encoder as hls_mod
    monkeypatch.setattr(hls_mod, "hls_manager", hls_falso)
    monkeypatch.setattr(sup_mod.banco, "cameras_listar", lambda: [])
    return hls_falso


def test_nao_revisa_de_novo_antes_do_intervalo(monkeypatch):
    hls_falso = _preparar(monkeypatch)
    relogio = {"agora": 1_000_000.0}
    monkeypatch.setattr(sup_mod.time, "time", lambda: relogio["agora"])

    sv = WorkerSupervisor()
    sv._revisar_hls()
    assert hls_falso.chamadas_revisar == 1

    relogio["agora"] += 5.0   # um tick normal do loop de 5s
    sv._revisar_hls()
    assert hls_falso.chamadas_revisar == 1, "não pode revisar de novo antes do intervalo"


def test_revisa_de_novo_apos_o_intervalo(monkeypatch):
    hls_falso = _preparar(monkeypatch)
    relogio = {"agora": 1_000_000.0}
    monkeypatch.setattr(sup_mod.time, "time", lambda: relogio["agora"])

    sv = WorkerSupervisor()
    sv._revisar_hls()
    assert hls_falso.chamadas_revisar == 1

    relogio["agora"] += _INTERVALO_HLS_SEG + 0.1
    sv._revisar_hls()
    assert hls_falso.chamadas_revisar == 2


def test_hls_inativo_nao_consulta_o_banco_nem_conta_pro_intervalo(monkeypatch):
    hls_falso = _preparar(monkeypatch)
    hls_falso._ativo = False
    chamou_banco = []
    monkeypatch.setattr(sup_mod.banco, "cameras_listar", lambda: chamou_banco.append(1))
    sv = WorkerSupervisor()

    sv._revisar_hls()

    assert chamou_banco == []
    assert hls_falso.chamadas_revisar == 0
