"""Regressão: quando o FFmpeg não encerra a tempo (`proc.wait(timeout=3)` estoura) e o
código cai para `proc.kill()`, faltava um `proc.wait()` subsequente para "colher" o
processo morto. Em Linux isso deixa um zumbi (defunct) até outro `Popen()` qualquer
disparar limpeza lazy. O `finally` de `_Encoder._loop` precisa chamar `wait()` de novo
depois do `kill()`, sem deixar a função travada se esse segundo `wait()` também falhar.
"""
from __future__ import annotations
import subprocess

from app.streaming.hls_encoder import _Encoder


class _FakeStdin:
    def close(self) -> None:
        pass


class _FakeProc:
    """Simula um FFmpeg que ignora o `stdin.close()` e não morre a tempo do primeiro
    `wait()` — obrigando o código a cair no `kill()`."""

    def __init__(self, *, segundo_wait_falha: bool = False) -> None:
        self.stdin = _FakeStdin()
        self.kill_chamado = False
        self.wait_chamadas = 0
        self._segundo_wait_falha = segundo_wait_falha
        self._morto = False

    def poll(self):
        return 0 if self._morto else None

    def wait(self, timeout=None):
        self.wait_chamadas += 1
        if self.wait_chamadas == 1:
            # primeiro wait: estoura o timeout, como um FFmpeg preso
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout)
        if self._segundo_wait_falha:
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout)
        self._morto = True
        return 0

    def kill(self) -> None:
        self.kill_chamado = True


def _rodar_uma_iteracao(encoder: _Encoder, fake_proc: _FakeProc, *, alimentar_levanta=True):
    """Substitui as partes que tocam câmera/FFmpeg de verdade e roda `_loop` até a
    primeira (e única) passagem pelo `finally`, sem os 5s de espera entre tentativas."""
    encoder._aguardar_dimensoes = lambda timeout: (10, 10)
    encoder._criar_ffmpeg = lambda w, h: fake_proc

    def _alimentar_fake(proc, w, h):
        # a "câmera parou" bem quando a alimentação começa — não precisamos do
        # sleep(5) de retry entre tentativas do loop externo.
        encoder._stop.set()
        if alimentar_levanta:
            raise RuntimeError("simulando falha de alimentação")

    encoder._alimentar = _alimentar_fake
    encoder._loop()


def test_kill_e_seguido_de_wait_para_colher_o_processo():
    encoder = _Encoder(camera_id=1)
    fake_proc = _FakeProc()

    _rodar_uma_iteracao(encoder, fake_proc)

    assert fake_proc.kill_chamado is True
    assert fake_proc.wait_chamadas == 2, (
        "esperava um wait() inicial (que estoura) e um segundo wait() de reap após "
        "o kill() — sem o segundo, o processo morto vira zumbi"
    )


def test_segundo_wait_falhando_nao_trava_nem_propaga():
    """Mesmo se o wait() de reap também falhar, `_loop` precisa retornar normalmente
    (o kill() já garantiu a morte do processo; o wait() é só para reaping)."""
    encoder = _Encoder(camera_id=2)
    fake_proc = _FakeProc(segundo_wait_falha=True)

    _rodar_uma_iteracao(encoder, fake_proc)  # não deve levantar nem travar

    assert fake_proc.kill_chamado is True
    assert fake_proc.wait_chamadas == 2
