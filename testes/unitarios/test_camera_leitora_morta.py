"""A câmera que morre em silêncio — o achado K1 da auditoria de 27/08/2026.

Dois defeitos que só eram fatais juntos:

  1. `_reader_loop` saía por `except: break` sem zerar `_ultimo_frame` e sem logar. A partir
     daí `ler()` devolvia o último quadro bom PARA SEMPRE.
  2. O pipeline, ao ver o mesmo objeto de frame, republicava carimbando `time.time()` para
     manter `ultimo_frame_ts` andando.

Resultado: imagem nítida e "ao vivo" na tela, congelada; o supervisor nunca reiniciava porque
o relógio de frescor nunca envelhecia; e nenhuma linha no log. Era o único achado do laudo
totalmente invisível — daí este arquivo.

O que se trava aqui é o CONTRATO entre os dois: leitora morta ⇒ `ler()` devolve None, e
republicação NÃO rejuvenesce o relógio da fonte.
"""
from __future__ import annotations

import threading
import time

import pytest

from app.visao.camera import Camera


class _CapQueExplode:
    """VideoCapture que entrega N quadros e então levanta — a câmera que some da rede."""

    def __init__(self, ate_explodir: int = 2):
        self.ate_explodir = ate_explodir
        self.leituras = 0

    def read(self):
        self.leituras += 1
        if self.leituras > self.ate_explodir:
            raise RuntimeError("stream caiu")
        return True, [[self.leituras]]

    def release(self):
        pass


def _camera_com(cap) -> Camera:
    cam = Camera(tipo="rtsp")
    cam.cap = cap
    return cam


def _rodar_leitora(cam: Camera, ate=lambda c: True, limite_seg: float = 2.0) -> None:
    """Roda `_reader_loop` numa thread e espera ela morrer sozinha."""
    t = threading.Thread(target=cam._reader_loop, daemon=True)
    t.start()
    fim = time.time() + limite_seg
    while t.is_alive() and time.time() < fim:
        time.sleep(0.01)
    assert not t.is_alive(), "a leitora não encerrou — o teste não mede o que diz medir"


class TestLeitoraQueMorre:
    def test_excecao_zera_o_frame_em_vez_de_servir_o_antigo(self):
        """O coração do K1: sem isto, `ler()` devolve o quadro velho indefinidamente e
        nada no sistema consegue perceber que a câmera parou."""
        cam = _camera_com(_CapQueExplode(ate_explodir=2))
        _rodar_leitora(cam)
        assert cam.ler() is None

    def test_excecao_e_logada_como_erro(self, caplog):
        """Silencioso era o que tornava a câmera congelada indistinguível de uma saudável."""
        cam = _camera_com(_CapQueExplode(ate_explodir=1))
        with caplog.at_level("ERROR"):
            _rodar_leitora(cam)
        assert any("Thread leitora encerrada" in r.message for r in caplog.records), \
            "a morte da leitora tem de deixar rastro no log"

    def test_relogio_da_fonte_para_de_andar(self):
        """`ultimo_frame_em` é o que o watchdog passa a consultar. Ele tem de congelar no
        último quadro REAL, não acompanhar o relógio de parede."""
        cam = _camera_com(_CapQueExplode(ate_explodir=2))
        _rodar_leitora(cam)
        parou_em = cam.ultimo_frame_em()
        assert parou_em > 0, "pré-condição: a câmera chegou a entregar quadro"
        time.sleep(0.05)
        assert cam.ultimo_frame_em() == parou_em

    def test_parada_normal_nao_loga_erro(self, caplog):
        """O caminho feliz não pode virar ruído — senão o log volta a mentir (M1)."""
        class _CapOk:
            def read(self):
                time.sleep(0.005)
                return True, [[1]]
            def release(self):
                pass

        cam = _camera_com(_CapOk())
        t = threading.Thread(target=cam._reader_loop, daemon=True)
        with caplog.at_level("ERROR"):
            t.start()
            time.sleep(0.05)
            cam._parar_leitura.set()
            t.join(timeout=1.0)
        assert not t.is_alive()
        assert not [r for r in caplog.records if "Thread leitora" in r.message]

    def test_cap_none_tambem_zera(self):
        """Sair porque outra thread liberou o cap tem o mesmo dever: não servir quadro velho."""
        class _CapLento:
            def read(self):
                time.sleep(0.005)
                return True, [[1]]
            def release(self):
                pass

        cam = _camera_com(_CapLento())
        t = threading.Thread(target=cam._reader_loop, daemon=True)
        t.start()
        time.sleep(0.05)
        assert cam.ler() is not None, "pré-condição: estava entregando"
        cam.cap = None
        t.join(timeout=1.0)
        assert not t.is_alive()
        assert cam.ler() is None


class TestTimeoutDeLeituraRTSP:
    """K2 — o backend FFmpeg recusa `CAP_PROP_READ_TIMEOUT_MSEC`, então o teto real tem de
    viajar na variável de ambiente, que é a via que ele honra. Sem isso a leitora fica presa
    em `cap.read()` para sempre e a câmera vira irrecuperável (medido: 363 s fora)."""

    def test_env_de_captura_leva_timeout_alem_do_transporte(self, monkeypatch):
        import os
        import app.visao.camera as cam_mod

        capturado = {}

        class _CapFalso:
            def isOpened(self):
                return True
            def set(self, *a):
                return True

        def _fake_videocapture(origem, backend=None):
            capturado["env"] = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS", "")
            return _CapFalso()

        monkeypatch.setattr(cam_mod.cv2, "VideoCapture", _fake_videocapture)
        monkeypatch.setattr(cam_mod.threading, "Thread", lambda **kw: type(
            "T", (), {"start": lambda self: None})())

        cam = Camera(tipo="rtsp", intelbras={"host": "10.0.0.9", "rtsp_transporte": "tcp"})
        cam.abrir()

        env = capturado["env"]
        assert "rtsp_transport;tcp" in env, "o transporte não pode ter se perdido"
        # Os dois nomes: `stimeout` no FFmpeg 4, `timeout` no 5+. A opção que o binário não
        # conhece é ignorada, então mandar ambos é o que faz o teto valer nas duas versões.
        assert "stimeout;%d" % cam_mod.RTSP_TIMEOUT_USEC in env
        assert "timeout;%d" % cam_mod.RTSP_TIMEOUT_USEC in env

    def test_timeout_e_maior_que_o_best_effort_da_propriedade(self):
        """`CAP_PROP_READ_TIMEOUT_MSEC` são 4 s. O teto de socket tem de ser o ÚLTIMO a
        disparar, senão ele mascara o caminho que funciona quando o backend coopera."""
        import app.visao.camera as cam_mod
        assert cam_mod.RTSP_TIMEOUT_USEC / 1_000_000 > 4.0

    def test_join_da_leitora_espera_mais_que_o_teto_de_socket(self):
        """`fechar()` precisa dar à leitora tempo de ver o timeout e sair sozinha; se
        desistir antes, ela recusa o `release()` e a conexão vaza sem necessidade."""
        import app.visao.camera as cam_mod
        assert cam_mod.TIMEOUT_JOIN_LEITORA_SEG > cam_mod.RTSP_TIMEOUT_USEC / 1_000_000
