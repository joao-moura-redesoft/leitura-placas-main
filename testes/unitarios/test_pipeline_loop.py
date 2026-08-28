"""Cadência do loop de captura (`Pipeline._loop`) — sem câmera, sem detector, sem OCR.

`Pipeline.__new__(Pipeline)` bypassa `__init__` (que criaria Camera/detector/OCR/tracker
de verdade) e preenche só o que `_loop` toca. Um relógio virtual substitui `time.time`/
`time.sleep` em `app.visao.pipeline`: `sleep()` só avança um contador sem bloquear de
verdade, então a suíte roda em milissegundos e as asserções de cadência são EXATAS.

O que este arquivo protege, em uma frase: ajuste de ambiente + publicação de frame +
detecção têm que andar sempre JUNTOS (nunca mistura frame cru com ajustado no stream/
OCR de produção), frame duplicado da câmera não pode gerar trabalho duplicado, e nada
disso pode atrasar a detecção de câmera morta.
"""
from __future__ import annotations
import threading

import numpy as np
import pytest

from app.core import estado
from app.visao import pipeline as pipeline_mod
from app.visao.pipeline import Pipeline
from app.web.leitura import FRAME_MAX_IDADE_SEG


class _RelogioFalso:
    """`sleep()` só avança um contador — nada bloqueia de verdade."""

    def __init__(self, inicio: float = 1_000_000.0):
        self.agora = inicio

    def time(self) -> float:
        return self.agora

    def sleep(self, segundos: float) -> None:
        if segundos > 0:
            self.agora += segundos


class _CameraFalsa:
    """`ler()` devolve sempre o MESMO objeto até `definir()` trocar — como a Camera
    real (app/visao/camera.py), que só guarda o último frame lido pela thread leitora."""

    def __init__(self):
        self.frame_atual = None
        self.chamadas_ler = 0
        self.chamadas_reconectar = 0
        self.reconectar_resultado = True
        # Relogio da FONTE, como na Camera real: so anda quando um quadro NOVO chega.
        # Sem isto o duble nao consegue distinguir "republicou o antigo" de "entregou
        # um novo", que e justamente a diferenca que o watchdog precisa enxergar.
        self.entregue_em = 0.0

    def ler(self):
        self.chamadas_ler += 1
        return self.frame_atual

    def ultimo_frame_em(self) -> float:
        return self.entregue_em

    def definir(self, frame) -> None:
        self.frame_atual = frame
        self.entregue_em = pipeline_mod.time.time()

    def reconectar(self, tentativas: int = 2) -> bool:
        self.chamadas_reconectar += 1
        return self.reconectar_resultado


class _AjustadorFalso:
    """`processar()` devolve um array NOVO (nunca o mesmo objeto) com uma marca
    reconhecível — é o que permite provar que TODO frame publicado passou por aqui,
    e nenhum frame cru vazou para `estado`."""

    ativo = True

    def __init__(self):
        self.chamadas = 0

    def processar(self, frame):
        self.chamadas += 1
        marcado = frame.copy()
        marcado[0, 0, 0] = 77
        return marcado

    @staticmethod
    def foi_marcado(frame) -> bool:
        return frame is not None and int(frame[0, 0, 0]) == 77


def _novo_frame(valor=1):
    return np.full((4, 4, 3), valor, dtype=np.uint8)


def _pipeline_de_teste(*, camera, ajustador, deteccao_fps_max=5, camera_fps=15, camera_db_id=1):
    p = Pipeline.__new__(Pipeline)
    p.camera_db_id = camera_db_id
    p.cfg = {"camera_fps": str(camera_fps)}
    p.camera = camera
    p.ajustador = ajustador
    p.deteccao_automatica = True
    p._intervalo_deteccao = 1.0 / max(1, int(deteccao_fps_max))
    p._ultima_deteccao = 0.0
    p._parar = threading.Event()
    p.chamadas_processar_frame = []
    # `_processar_frame` recebe DOIS arrays: o que vai virar MJPEG (e recebe os
    # retângulos) e o quadro limpo que vai para OCR/disco. `chamadas_processar_frame`
    # segue sendo o primeiro — é o publicado, que é o que as asserções de cadência e de
    # ajuste medem; o segundo fica em lista própria para o teste do invariante.
    p.chamadas_frame_limpo = []
    def _dube_processar(frame, frame_limpo):
        p.chamadas_processar_frame.append(frame)
        p.chamadas_frame_limpo.append(frame_limpo)
    p._processar_frame = _dube_processar
    return p


@pytest.fixture
def relogio(monkeypatch):
    r = _RelogioFalso()
    monkeypatch.setattr(pipeline_mod.time, "time", r.time)
    monkeypatch.setattr(pipeline_mod.time, "sleep", r.sleep)
    return r


@pytest.fixture
def espiao_publicacao(monkeypatch, relogio):
    """Guarda (instante_virtual, frame) de cada chamada a `registrar_frame_camera`.

    Não dá pra usar `estado.ultimo_frame_ts` aqui: o `time` de app/core/estado.py não
    está mockado, então mediria tempo de PAREDE real — que não passa de verdade,
    já que o `sleep()` virtual não bloqueia. O relógio virtual é a única referência
    de tempo válida dentro destes testes.
    """
    chamadas = []
    original = estado.registrar_frame_camera

    def _espiao(camera_id, frame, ts=None):
        chamadas.append((relogio.agora, frame))
        return original(camera_id, frame, ts)

    monkeypatch.setattr(estado, "registrar_frame_camera", _espiao)
    return chamadas


@pytest.fixture(autouse=True)
def _limpar_estado_da_camera_de_teste():
    yield
    for cam_id in (1, 2, 3):
        with estado.lock:
            estado.frames_cameras.pop(cam_id, None)
            estado.frames_cameras_limpos.pop(cam_id, None)
            estado.ultimo_frame_ts.pop(cam_id, None)


def _rodar_por(pinst, relogio, segundos: float):
    """Roda `_loop()` até o relógio virtual avançar `segundos` — pode ser chamado
    várias vezes seguidas no MESMO pipeline/câmera para simular fases (ex.: câmera
    muda de frame no meio do teste).

    Gancho em `camera.ler()` (chamado uma vez, incondicionalmente, no topo de toda
    iteração) pra marcar `_parar` assim que o alvo for atingido — a iteração corrente
    ainda termina (o `while not self._parar.is_set()` só é checado no topo da
    PRÓXIMA), então o tempo final pode passar um pouco do alvo; isso é esperado e as
    asserções de contagem toleram essa folga de +-1 iteração.

    `_parar.clear()` no início é essencial: `_loop()` já retornou uma vez (setou
    `_parar`) na chamada anterior, e um `threading.Event` fica "set" até alguém
    limpar — sem isto, a segunda chamada acharia `_parar` já ativo e `_loop()`
    devolveria na hora, sem rodar UMA iteração sequer.

    `_ler_base_de_teste` guarda o `camera.ler` ORIGINAL (real ou já customizado pelo
    teste) na PRIMEIRA chamada, e toda chamada seguinte envolve esse mesmo original —
    nunca o wrapper da chamada anterior. Sem isso, chamar `_rodar_por` duas vezes no
    mesmo pipeline empilharia wrappers: o de dentro carregaria um `alvo` já ULTRAPASSADO
    (da rodada passada), e como `relogio.agora` só cresce, esse wrapper velho dispararia
    `_parar.set()` na primeira leitura da rodada nova — encerrando o loop depois de
    UMA iteração só, mascarando qualquer coisa que a segunda rodada deveria exercitar.
    """
    pinst._parar.clear()
    alvo = relogio.agora + segundos
    base = getattr(pinst, "_ler_base_de_teste", None)
    if base is None:
        base = pinst.camera.ler
        pinst._ler_base_de_teste = base

    def _ler_com_parada():
        if relogio.agora >= alvo:
            pinst._parar.set()
        return base()

    pinst.camera.ler = _ler_com_parada
    pinst._loop()


class TestCadencia:
    def test_deteccao_roda_na_taxa_configurada_nao_na_da_camera(self, relogio):
        """15 fps de câmera, 5 fps de detecção, 3s virtuais -> ~15 ticks de
        detecção (3*5), não ~45 (3*15)."""
        cam = _CameraFalsa()
        cam.definir(_novo_frame())
        aj = _AjustadorFalso()
        pinst = _pipeline_de_teste(camera=cam, ajustador=aj, deteccao_fps_max=5, camera_fps=15)

        # Câmera entrega um frame NOVO a cada leitura (senão C entraria em jogo e
        # reduziria as contagens abaixo) — isolando o que este teste quer provar.
        contador = [0]
        ler_original = cam.ler

        def _ler_com_frame_novo():
            contador[0] += 1
            cam.frame_atual = _novo_frame(contador[0])
            return ler_original()

        cam.ler = _ler_com_frame_novo
        _rodar_por(pinst, relogio, 3.0)

        # Tolerância larga de propósito: 15 (camera_fps) é múltiplo EXATO de 5
        # (deteccao_fps_max), e comparar diferenças de ponto flutuante bem em cima
        # de uma razão exata (`agora - ultima >= intervalo`) é sensível ao
        # arredondamento acumulado de somar 1/15 repetidamente — medido: o número
        # real de ticks em 3s cai por volta de 12-14, não os 15 "ideais". Isso
        # acontece com timestamps reais também (não é artefato só do relógio
        # falso); o que importa aqui é a ORDEM DE GRANDEZA — bem menor que os ~45
        # reads de câmera, nunca perto disso.
        assert 10 <= len(pinst.chamadas_processar_frame) <= 16
        assert 42 <= cam.chamadas_ler <= 48

    def test_nada_e_publicado_entre_ticks(self, relogio, espiao_publicacao):
        """`registrar_frame_camera` só é chamado nos ticks de detecção — não a cada
        leitura de câmera."""
        cam = _CameraFalsa()
        cam.definir(_novo_frame())
        aj = _AjustadorFalso()
        pinst = _pipeline_de_teste(camera=cam, ajustador=aj, deteccao_fps_max=5, camera_fps=15)
        _rodar_por(pinst, relogio, 2.0)

        # camera.ler() roda ~30x (2s*15) mas a publicação só ~10x (2s*5).
        assert abs(cam.chamadas_ler - 30) <= 3
        assert abs(len(espiao_publicacao) - 10) <= 2


class TestInvarianteDoAjuste:
    """O invariante central de B: ajuste, publicação e detecção SEMPRE juntos —
    nunca um frame cru (sem a marca do ajustador) chega em `estado`."""

    def test_ajustador_e_chamado_o_mesmo_numero_de_vezes_que_a_deteccao(self, relogio):
        cam = _CameraFalsa()
        contador = [0]

        def _ler_com_frame_novo():
            contador[0] += 1
            cam.frame_atual = _novo_frame(contador[0])
            return _CameraFalsa.ler(cam)

        cam.ler = _ler_com_frame_novo
        aj = _AjustadorFalso()
        pinst = _pipeline_de_teste(camera=cam, ajustador=aj, deteccao_fps_max=5, camera_fps=15)
        _rodar_por(pinst, relogio, 3.0)

        assert aj.chamadas == len(pinst.chamadas_processar_frame)
        assert aj.chamadas > 0

    def test_todo_frame_publicado_carrega_a_marca_do_ajustador(self, relogio, espiao_publicacao):
        cam = _CameraFalsa()
        contador = [0]

        def _ler_com_frame_novo():
            contador[0] += 1
            cam.frame_atual = _novo_frame(contador[0])
            return _CameraFalsa.ler(cam)

        cam.ler = _ler_com_frame_novo
        aj = _AjustadorFalso()
        pinst = _pipeline_de_teste(camera=cam, ajustador=aj, deteccao_fps_max=5, camera_fps=15)
        _rodar_por(pinst, relogio, 2.0)

        assert len(espiao_publicacao) > 0
        for _instante, frame in espiao_publicacao:
            assert _AjustadorFalso.foi_marcado(frame), "frame publicado sem passar pelo ajustador"

    def test_frame_limpo_tambem_carrega_a_marca(self, relogio):
        """`registrar_frame_camera_limpo` é o que `ler_placa`/`frame_ao_vivo` usam —
        se ele recebesse frame cru, o OCR de produção degradaria em silêncio."""
        cam = _CameraFalsa()
        cam.definir(_novo_frame())
        aj = _AjustadorFalso()
        pinst = _pipeline_de_teste(camera=cam, ajustador=aj, deteccao_fps_max=5, camera_fps=15,
                                   camera_db_id=2)
        _rodar_por(pinst, relogio, 0.5)

        limpo = estado.obter_frame_camera_limpo(2)
        assert _AjustadorFalso.foi_marcado(limpo)

    def test_quadro_do_stream_e_o_limpo_sao_arrays_separados(self, relogio):
        """O que `_processar_frame` recebe como 1º argumento é rabiscado com os
        retângulos e vai para o MJPEG; o 2º é o que vira recorte de OCR, snapshot e
        amostra de dataset. Se os dois forem o MESMO array, todo snapshot gravado sai
        com a caixa verde desenhada por cima da placa — que era o comportamento antigo,
        visível nas miniaturas do histórico.
        """
        cam = _CameraFalsa()
        cam.definir(_novo_frame())
        aj = _AjustadorFalso()
        pinst = _pipeline_de_teste(camera=cam, ajustador=aj, deteccao_fps_max=5, camera_fps=15)
        _rodar_por(pinst, relogio, 1.0)

        assert pinst.chamadas_processar_frame, "nada foi processado — teste não mediu nada"
        for saida, limpo in zip(pinst.chamadas_processar_frame, pinst.chamadas_frame_limpo):
            assert saida is not limpo
            # Separados, mas partindo do mesmo conteúdo: a cópia é feita DEPOIS do
            # ajuste de ambiente, senão o OCR receberia um quadro sem o ajuste.
            assert np.array_equal(saida, limpo)


class TestFrameDuplicado:
    def test_camera_parada_processa_uma_vez_mas_republica_a_cada_tick(self, relogio, espiao_publicacao):
        """Câmera trava (mesmo objeto sempre) por 2s: detecção roda 1x só, mas a
        publicação continua nos ticks — `ultimo_frame_ts` não pode parecer com
        câmera morta só porque a detecção foi pulada."""
        cam = _CameraFalsa()
        cam.definir(_novo_frame())   # nunca muda
        aj = _AjustadorFalso()
        pinst = _pipeline_de_teste(camera=cam, ajustador=aj, deteccao_fps_max=5, camera_fps=15)
        _rodar_por(pinst, relogio, 2.0)

        assert len(pinst.chamadas_processar_frame) == 1
        assert aj.chamadas == 1
        assert abs(len(espiao_publicacao) - 10) <= 2   # publica em todo tick mesmo assim

    def test_frame_novo_no_meio_da_travada_e_processado(self, relogio):
        """Depois de travar, a câmera volta a entregar frame novo — tem que
        processar de novo (não fica presa no primeiro `ultimo_bruto` para sempre)."""
        cam = _CameraFalsa()
        cam.definir(_novo_frame(1))
        aj = _AjustadorFalso()
        pinst = _pipeline_de_teste(camera=cam, ajustador=aj, deteccao_fps_max=5, camera_fps=15)
        _rodar_por(pinst, relogio, 1.0)
        assert len(pinst.chamadas_processar_frame) == 1

        cam.definir(_novo_frame(2))
        _rodar_por(pinst, relogio, 1.0)
        assert len(pinst.chamadas_processar_frame) == 2


class TestFrescor:
    def test_intervalo_entre_publicacoes_nunca_passa_do_limite_de_frescor(self, relogio, espiao_publicacao):
        """Guarda contra regressão: se o intervalo de publicação crescer demais,
        `frame_ao_vivo` (app/web/leitura.py) desiste e cai pra conexão RTSP direta —
        que falha, porque a câmera Intelbras já está ocupada pelo pipeline."""
        cam = _CameraFalsa()
        cam.definir(_novo_frame())
        aj = _AjustadorFalso()
        pinst = _pipeline_de_teste(camera=cam, ajustador=aj, deteccao_fps_max=5, camera_fps=15)
        _rodar_por(pinst, relogio, 5.0)

        instantes = [t for t, _ in espiao_publicacao]
        gaps = [b - a for a, b in zip(instantes, instantes[1:])]
        assert gaps, "nenhuma publicação registrada"
        assert max(gaps) < FRAME_MAX_IDADE_SEG

    def test_frame_novo_carimba_o_relogio_da_fonte_nao_o_de_agora(self, relogio):
        """Achado do review de 28/08/2026: o branch de frame NOVO carimbava
        `time.time()` no fim do ajuste+detecção+OCR, não o instante em que a FONTE
        entregou o quadro — sob processamento lento (troca de modelo, contenção,
        soluço de GPU), o relógio de frescor andaria mais devagar que a câmera sem
        nenhuma câmera parada de verdade, o mesmo padrão de falha que motivou
        `Camera.ultimo_frame_em()` existir."""
        cam = _CameraFalsa()
        cam.definir(_novo_frame())
        entregue_em = cam.entregue_em

        aj = _AjustadorFalso()
        pinst = _pipeline_de_teste(camera=cam, ajustador=aj, deteccao_fps_max=5, camera_fps=15)

        def _processar_devagar(frame, frame_limpo):
            relogio.sleep(3.0)   # simula deteccao+OCR lentos
            pinst.chamadas_processar_frame.append(frame)
            pinst.chamadas_frame_limpo.append(frame_limpo)
        pinst._processar_frame = _processar_devagar

        _rodar_por(pinst, relogio, 3.5)

        assert pinst.chamadas_processar_frame, "nenhum frame processado"
        assert estado.ultimo_frame_ts[pinst.camera_db_id] == entregue_em, (
            "o carimbo de frescor tem de ser o relógio da FONTE, não 'agora' depois "
            "de um processamento que levou 3s"
        )


class TestReconexao:
    def test_camera_morta_reconecta_na_cadencia_da_camera_nao_na_de_deteccao(self, relogio):
        """`deteccao_fps_max` baixo (1/s) não pode atrasar a detecção de câmera
        morta — o polling de reconexão (0.1s) é independente do portão de detecção."""
        cam = _CameraFalsa()
        cam.frame_atual = None   # câmera "morta" desde o início
        aj = _AjustadorFalso()
        pinst = _pipeline_de_teste(camera=cam, ajustador=aj, deteccao_fps_max=1, camera_fps=15)

        alvo_chamadas = 1

        def _parar_apos_reconectar():
            if cam.chamadas_reconectar >= alvo_chamadas:
                pinst._parar.set()
            return None

        cam.ler = _parar_apos_reconectar
        pinst._loop()

        assert cam.chamadas_reconectar == 1
        # reconectar() só é tentado depois de ~3s "sem frame" (comentário no _loop);
        # nada aqui depende de deteccao_fps_max=1 (que daria só 1 tick em 3s).
        assert relogio.agora >= 1_000_000.0 + 3.0

    def test_frame_de_antes_da_queda_nao_e_republicado_apos_reconectar(self, relogio, espiao_publicacao):
        cam = _CameraFalsa()
        cam.definir(_novo_frame(1))
        aj = _AjustadorFalso()
        pinst = _pipeline_de_teste(camera=cam, ajustador=aj, deteccao_fps_max=5, camera_fps=15)
        _rodar_por(pinst, relogio, 0.5)   # processa e publica o frame 1
        # Dentro dessa janela (0.5s / _intervalo_deteccao=0.2s) mais de um tick
        # acontece com o MESMO frame parado — os ticks depois do primeiro caem na
        # rota de duplicado (C) e corretamente REPUBLICAM o valor 1. Isso é esperado
        # aqui; o que este teste protege é o que vem DEPOIS da queda/reconexão.
        publicacoes_antes_da_queda = len(espiao_publicacao)
        assert publicacoes_antes_da_queda >= 1
        assert all(int(f[1, 1, 1]) == 1 for _, f in espiao_publicacao)

        cam.frame_atual = None   # câmera cai

        def _reconectar_com_frame_novo(tentativas=2):
            cam.chamadas_reconectar += 1
            cam.frame_atual = _novo_frame(2)
            return True

        cam.reconectar = _reconectar_com_frame_novo
        _rodar_por(pinst, relogio, 4.0)   # atravessa os 3s até reconectar

        assert cam.chamadas_reconectar == 1
        # A partir daqui (depois da queda), NENHUMA publicação pode carregar o valor
        # do frame de ANTES (1) — só o frame novo pós-reconexão (2).
        valores_depois_da_queda = [int(f[1, 1, 1]) for _, f in espiao_publicacao[publicacoes_antes_da_queda:]]
        assert valores_depois_da_queda, "nenhuma publicação depois da reconexão"
        assert set(valores_depois_da_queda) == {2}, (
            f"republicou o frame de antes da queda: {valores_depois_da_queda}")


class TestCasosDeBorda:
    def test_deteccao_fps_max_maior_que_camera_fps_processa_todo_frame_novo(self, relogio):
        """30 > 15: o portão fica sempre aberto — vira o comportamento antigo (toda
        leitura de câmera é um tick), sem processar nada duas vezes."""
        cam = _CameraFalsa()
        contador = [0]

        def _ler_com_frame_novo():
            contador[0] += 1
            cam.frame_atual = _novo_frame(contador[0])
            return _CameraFalsa.ler(cam)

        cam.ler = _ler_com_frame_novo
        aj = _AjustadorFalso()
        pinst = _pipeline_de_teste(camera=cam, ajustador=aj, deteccao_fps_max=30, camera_fps=15)
        _rodar_por(pinst, relogio, 2.0)

        assert len(pinst.chamadas_processar_frame) == cam.chamadas_ler
        vistos = [id(f) for f in pinst.chamadas_processar_frame]
        assert len(vistos) == len(set(vistos))

    def test_deteccao_automatica_desligada_nao_toca_camera_nem_estado(self, relogio):
        cam = _CameraFalsa()
        aj = _AjustadorFalso()
        pinst = _pipeline_de_teste(camera=cam, ajustador=aj)
        pinst.deteccao_automatica = False

        chamadas_sleep = [0]
        sleep_original = relogio.sleep

        def _sleep_e_parar(segundos):
            chamadas_sleep[0] += 1
            if chamadas_sleep[0] >= 3:
                pinst._parar.set()
            return sleep_original(segundos)

        monkeypatch_sleep = pipeline_mod.time.sleep
        pipeline_mod.time.sleep = _sleep_e_parar
        try:
            pinst._loop()
        finally:
            pipeline_mod.time.sleep = monkeypatch_sleep

        assert cam.chamadas_ler == 0
        assert aj.chamadas == 0
        assert len(pinst.chamadas_processar_frame) == 0

    def test_excecao_no_processamento_nao_derruba_o_loop(self, relogio, monkeypatch):
        cam = _CameraFalsa()
        cam.definir(_novo_frame())
        aj = _AjustadorFalso()
        pinst = _pipeline_de_teste(camera=cam, ajustador=aj, deteccao_fps_max=5, camera_fps=15)

        chamadas = [0]

        def _processar_com_falha_na_primeira(frame, frame_limpo):
            chamadas[0] += 1
            if chamadas[0] == 1:
                raise RuntimeError("falha simulada de detector")

        pinst._processar_frame = _processar_com_falha_na_primeira
        _rodar_por(pinst, relogio, 2.0)

        assert chamadas[0] >= 2, "o loop deveria ter se recuperado e tentado de novo"
