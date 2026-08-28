"""`Camera.fechar()` não pode liberar o cap com a thread leitora ainda dentro de `read()`.

Liberar ali derruba o PROCESSO INTEIRO com access violation no Windows+FFmpeg+RTSP —
crash nativo, sem traceback Python: o servidor some. O `join` da leitora sempre esteve em
`fechar()`, com um comentário explicando justamente esse perigo, mas o RESULTADO dele era
ignorado: esgotado o timeout, o código seguia para o `release()` do mesmo jeito. Bastava a
leitora estar presa (RTSP remoto instável, câmera fora da rede) para editar/remover uma
câmera, ou pedir uma leitura de bico, matar o servidor.

O contrato agora é o mesmo de `Pipeline.parar()`: devolve False SEM liberar nada, e quem
chama não trata a câmera como livre. A conexão fica retida até a próxima tentativa — vazar
um socket por alguns segundos é incomparavelmente melhor que perder o processo.

O segundo perigo é mais silencioso: `capturar_frame_unico`, `capturar_teste` e o caminho de
leitura do bico criam uma `Camera` local e soltam a referência logo depois. Se `fechar()`
não liberou, o coletor de lixo destrói o `cv2.VideoCapture` — e o destrutor nativo faz
exatamente o release() proibido, agora fora de qualquer contexto que explique o crash. Por
isso a quarentena de `fechar_ou_adiar`.
"""
from __future__ import annotations

import threading

import pytest

from app.visao import camera as camera_mod
from app.visao.camera import Camera


class _CapPreso:
    """Dublê de `cv2.VideoCapture` cuja leitura só retorna quando o teste liberar.

    É o estado real que motiva tudo isto: `cap.read()` de um RTSP que parou de responder,
    com o timeout do backend não valendo (a propriedade é best-effort).
    """

    def __init__(self) -> None:
        self.destravar = threading.Event()
        self.releases = 0

    def read(self):
        self.destravar.wait(timeout=30)
        return False, None

    def release(self) -> None:
        self.releases += 1


@pytest.fixture
def _join_curto(monkeypatch):
    """Encurta a espera pela leitora — o caminho exercitado é o do timeout."""
    monkeypatch.setattr(camera_mod, "TIMEOUT_JOIN_LEITORA_SEG", 0.2)


@pytest.fixture
def _quarentena_limpa():
    """A lista de pendentes é estado de módulo; um teste não pode herdar o do outro."""
    with camera_mod._pendentes_lock:
        camera_mod._pendentes_fechar.clear()
    yield
    with camera_mod._pendentes_lock:
        camera_mod._pendentes_fechar.clear()


def _camera_com_leitora_presa(cap: _CapPreso) -> Camera:
    """Uma `Camera` com a leitora REAL rodando sobre um cap que não devolve frame."""
    cam = Camera(tipo="rtsp", indice="rtsp://exemplo/stream")
    cam.cap = cap
    cam._parar_leitura.clear()
    cam._reader = threading.Thread(target=cam._reader_loop, daemon=True)
    cam._reader.start()
    return cam


def test_nao_libera_cap_com_leitora_presa(_join_curto):
    """O caso que derrubava o processo: fechar enquanto `read()` não voltou."""
    cap = _CapPreso()
    cam = _camera_com_leitora_presa(cap)
    try:
        assert cam.fechar() is False, "fechar() precisa avisar que não foi seguro"
        assert cap.releases == 0, (
            "release() foi chamado com a leitora viva — é o access violation que "
            "mata o processo"
        )
        assert cam.cap is cap, "o cap tem de continuar referenciado para tentar depois"
    finally:
        cap.destravar.set()
        cam._reader.join(timeout=5)


def test_libera_quando_a_leitora_finalmente_morre(_join_curto):
    """A conexão retida não vaza para sempre: a tentativa seguinte a recolhe."""
    cap = _CapPreso()
    cam = _camera_com_leitora_presa(cap)

    assert cam.fechar() is False

    cap.destravar.set()          # o `read()` volta, a leitora nota `_parar_leitura` e sai
    cam._reader.join(timeout=5)
    assert not cam._reader.is_alive()

    assert cam.fechar() is True
    assert cap.releases == 1
    assert cam.cap is None


def test_fecha_normal_quando_a_leitora_responde(_join_curto):
    """Caminho comum, inalterado: leitora encerra, cap é liberado, devolve True."""
    cap = _CapPreso()
    cap.destravar.set()          # nunca trava
    cam = _camera_com_leitora_presa(cap)

    assert cam.fechar() is True
    assert cap.releases == 1
    assert cam.cap is None


def test_reconectar_nao_abre_segunda_conexao_com_leitora_presa(_join_curto, monkeypatch):
    """`reconectar` chama `fechar` primeiro. Com a leitora presa ele NÃO pode abrir:
    seriam duas conexões RTSP para a mesma câmera física (que a Intelbras recusa) e o
    cap antigo ficaria inalcançável, sem ninguém para liberá-lo."""
    cap = _CapPreso()
    cam = _camera_com_leitora_presa(cap)
    aberturas = []
    monkeypatch.setattr(cam, "abrir", lambda: aberturas.append(1))
    try:
        assert cam.reconectar() is False
        assert aberturas == [], "abriu uma segunda conexão por cima da leitora presa"
        assert cap.releases == 0
    finally:
        cap.destravar.set()
        cam._reader.join(timeout=5)


def test_camera_retida_fica_referenciada_pela_quarentena(_join_curto, _quarentena_limpa):
    """Sem a quarentena, a câmera de vida curta sairia de escopo e o coletor de lixo
    chamaria o release() proibido — o mesmo crash, só que depois e sem causa visível."""
    cap = _CapPreso()
    cam = _camera_com_leitora_presa(cap)
    try:
        camera_mod.fechar_ou_adiar(cam, "teste")
        assert cam in camera_mod._pendentes_fechar
        assert cap.releases == 0
    finally:
        cap.destravar.set()
        cam._reader.join(timeout=5)


def test_quarentena_e_drenada_na_proxima_captura(_join_curto, _quarentena_limpa):
    """A retenção é temporária: assim que a leitora morre, a chamada seguinte libera."""
    cap = _CapPreso()
    cam = _camera_com_leitora_presa(cap)
    camera_mod.fechar_ou_adiar(cam, "teste")
    assert camera_mod._pendentes_fechar == [cam]

    cap.destravar.set()
    cam._reader.join(timeout=5)

    # Qualquer fechamento seguinte drena os pendentes antes de cuidar do seu.
    outra = _camera_com_leitora_presa(_CapPreso())
    outra._parar_leitura.set()
    outra.cap.destravar.set()
    outra._reader.join(timeout=5)
    camera_mod.fechar_ou_adiar(outra, "teste")

    assert cam not in camera_mod._pendentes_fechar
    assert cap.releases == 1, "a conexão retida precisa ser liberada quando dá"


def test_pipeline_parar_propaga_a_recusa(_join_curto):
    """`Pipeline.parar()` já tinha o contrato "a thread confirmou que morreu?". A leitora
    da câmera é OUTRA thread, e a recusa dela também tem de chegar a `parar_camera` —
    senão a instância é desregistrada e ninguém volta para liberar a conexão."""
    from app.visao.pipeline import Pipeline

    p = Pipeline.__new__(Pipeline)      # sem __init__: carregaria modelos de verdade
    p.camera_db_id = 4242
    p._parar = threading.Event()
    p._thread = None

    cap = _CapPreso()
    p.camera = _camera_com_leitora_presa(cap)
    try:
        assert p.parar() is False
        assert cap.releases == 0
    finally:
        cap.destravar.set()
        p.camera._reader.join(timeout=5)
