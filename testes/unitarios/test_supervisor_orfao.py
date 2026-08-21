"""O supervisor como zelador de pipeline órfão, e a honestidade do log de reinício.

`Pipeline.parar()` passou a responder "a thread confirmou que morreu?", e
`parar_camera`/`reiniciar_camera` se recusam a prosseguir quando a resposta é falsa — abrir
a câmera com a thread antiga viva significaria uma SEGUNDA conexão RTSP concorrente, que a
Intelbras não aceita. Isso criou dois estados que ninguém tratava:

1. **Órfão.** `DELETE /api/cameras/{id}` apaga a linha e chama `parar_camera`. Se a thread
   não morre, a instância fica em `_instancias` retendo o RTSP — e o supervisor só
   registrava "não encontrada ou inativa — cancelando reinício", a cada ciclo, para
   sempre. A câmera física ficava presa até o processo reiniciar.

2. **Sucesso falso.** `_tentar_reiniciar` logava "pipeline reiniciado com sucesso" na linha
   seguinte à chamada, sem olhar o retorno. Quem lia o log concluía que a câmera havia
   voltado quando o reinício tinha sido abortado.
"""
from __future__ import annotations

import time

import pytest

import app.visao.pipeline as pipeline
from app.operacao.supervisor import WorkerSupervisor

CAM = 7777

# A fixture autouse `_sem_visao` (conftest.py) troca `parar_camera` por um no-op, para a
# suíte não tocar câmera. Aqui o alvo do teste É essa função, então guardamos a real antes
# do stub — mesmo padrão de `test_stream_cache.py::_PARAR_CAMERA_REAL`.
_PARAR_CAMERA_REAL = pipeline.parar_camera
_INICIAR_CAMERA_REAL = pipeline.iniciar_camera


class _PipelineFalso:
    """Dublê de `Pipeline` no que o supervisor e `parar_camera` tocam.

    `parou` é o contrato novo: False simula a thread que não confirmou morte (travada em
    leitura/reconexão de câmera), que é a condição que gera órfão.
    """

    def __init__(self, parou: bool = True):
        self._parou = parou
        self.camera_db_id = CAM
        self.deteccao_automatica = True
        self.iniciando = False
        self.tentativas_parar = 0

        class _Thread:
            def is_alive(self_inner):
                return True
        self._thread = _Thread()

        class _Camera:
            def fechar(self_inner):
                pass
        self.camera = _Camera()

    def parar(self) -> bool:
        self.tentativas_parar += 1
        return self._parou


@pytest.fixture
def limpo(monkeypatch):
    """Isola `_instancias` e devolve o `parar_camera` REAL, que é o alvo aqui."""
    monkeypatch.setattr(pipeline, "_instancias", {}, raising=False)
    monkeypatch.setattr(pipeline, "parar_camera", _PARAR_CAMERA_REAL)
    yield


def _supervisor(monkeypatch, cameras: list[dict]) -> WorkerSupervisor:
    """Supervisor com o cadastro dublado. `cameras=[]` = câmera saiu do banco.

    `monkeypatch.setattr` e não atribuição direta: `supervisor.banco` é atributo de módulo,
    e sem teardown o dublê vazaria para o resto da sessão — o próximo teste a chamar
    `WorkerSupervisor.health()` (via `GET /api/health`) veria a câmera fantasma daqui.
    """
    import app.operacao.supervisor as sup_mod

    class _BancoFalso:
        @staticmethod
        def cameras_listar():
            return cameras

    monkeypatch.setattr(sup_mod, "banco", _BancoFalso())
    s = WorkerSupervisor()
    s._cfg = {"camera_tipo": "rtsp"}
    return s


class TestOrfao:
    def test_camera_fora_do_cadastro_e_liberada_em_vez_de_cancelar(self, limpo, monkeypatch):
        """O caso que vazava conexão: linha apagada do banco, instância viva."""
        p = _PipelineFalso(parou=True)
        pipeline._instancias[CAM] = p

        s = _supervisor(monkeypatch, cameras=[])            # câmera não existe mais no cadastro
        s._tentar_reiniciar(CAM, time.time())

        assert p.tentativas_parar == 1, "o supervisor tem de TENTAR parar, não só logar"
        assert CAM not in pipeline._instancias, "instância órfã tinha de ser desregistrada"

    def test_thread_ainda_viva_mantem_a_instancia_para_a_proxima_volta(self, limpo, monkeypatch):
        """Não confirmou morte: NÃO desregistra (senão outra chamada acha a câmera livre
        e abre uma 2ª conexão RTSP), mas continua tentando nos ciclos seguintes."""
        p = _PipelineFalso(parou=False)
        pipeline._instancias[CAM] = p

        s = _supervisor(monkeypatch, cameras=[])
        s._tentar_reiniciar(CAM, time.time())
        assert CAM in pipeline._instancias, "não pode liberar a câmera com a thread viva"
        assert p.tentativas_parar == 1

        # Segundo ciclo, agora a thread morreu: o órfão finalmente sai.
        p._parou = True
        s._backoff_ate.pop(CAM, None)          # ignora o backoff, é o que o tempo faria
        s._tentar_reiniciar(CAM, time.time())
        assert CAM not in pipeline._instancias
        assert p.tentativas_parar == 2

    def test_camera_inativa_tambem_e_liberada(self, limpo, monkeypatch):
        """Desativada no cadastro é o mesmo problema: não há config para subir."""
        p = _PipelineFalso(parou=True)
        pipeline._instancias[CAM] = p

        s = _supervisor(monkeypatch, cameras=[{"id": CAM, "ativo": 0}])
        s._tentar_reiniciar(CAM, time.time())

        assert p.tentativas_parar == 1
        assert CAM not in pipeline._instancias


class TestLogDeReinicioNaoMente:
    def test_reinicio_abortado_nao_registra_sucesso(self, limpo, monkeypatch, caplog):
        """A regressão de diagnóstico: sucesso logado sobre um reinício que não houve."""
        pipeline._instancias[CAM] = _PipelineFalso(parou=True)
        monkeypatch.setattr(pipeline, "_cfg_para_camera", lambda cfg, cam: dict(cfg))
        monkeypatch.setattr(pipeline, "reiniciar_camera", lambda cid, cfg: False)

        s = _supervisor(monkeypatch, cameras=[{"id": CAM, "ativo": 1}])
        with caplog.at_level("INFO"):
            s._tentar_reiniciar(CAM, time.time())

        texto = caplog.text
        assert "reiniciado com sucesso" not in texto
        assert "NÃO confirmado" in texto

    def test_reinicio_bem_sucedido_continua_registrando_sucesso(self, limpo, monkeypatch, caplog):
        pipeline._instancias[CAM] = _PipelineFalso(parou=True)
        monkeypatch.setattr(pipeline, "_cfg_para_camera", lambda cfg, cam: dict(cfg))
        monkeypatch.setattr(pipeline, "reiniciar_camera", lambda cid, cfg: True)

        s = _supervisor(monkeypatch, cameras=[{"id": CAM, "ativo": 1}])
        with caplog.at_level("INFO"):
            s._tentar_reiniciar(CAM, time.time())

        assert "reiniciado com sucesso" in caplog.text


class TestCicloDeVidaSerializado:
    """`iniciar_camera`/`parar_camera`/`reiniciar_camera` faziam check-then-act sobre
    `_instancias` sem lock, e a janela entre checar e agir é de SEGUNDOS (join de 5s na
    parada, dezenas de segundos abrindo RTSP e carregando modelos no início).

    O cenário real: supervisor reiniciando a câmera 3 no mesmo intervalo em que um admin
    salva o cadastro dela — as duas threads pegam a mesma instância, as duas fazem `pop`, e
    as duas sobem um `Pipeline`. Dois pipelines, duas conexões RTSP, e o registro guardando
    só o último: o outro fica órfão e invisível.
    """

    def test_nao_sobe_segundo_pipeline_com_um_vivo(self, limpo, monkeypatch):
        """A guarda de último nível: fecha o caminho de `reiniciar` (salvar config), que
        chama `parar_todas()` e segue para `iniciar_cameras_db` mesmo com thread presa."""
        vivo = _PipelineFalso(parou=False)      # thread viva, não confirmou parada
        pipeline._instancias[CAM] = vivo
        monkeypatch.setattr(pipeline, "iniciar_camera", _INICIAR_CAMERA_REAL)
        criados = []
        monkeypatch.setattr(pipeline, "Pipeline",
                            lambda *a, **k: criados.append(1) or _PipelineFalso())

        pipeline.iniciar_camera(CAM, {"camera_tipo": "rtsp"})

        assert criados == [], "subiu um segundo pipeline com o anterior ainda vivo"
        assert pipeline._instancias[CAM] is vivo

    def test_o_lock_e_por_camera_e_nao_global(self):
        """Uma câmera subindo leva dezenas de segundos; travar as outras nesse período
        transformaria o conserto numa indisponibilidade."""
        assert pipeline._lock_ciclo(1) is pipeline._lock_ciclo(1)
        assert pipeline._lock_ciclo(1) is not pipeline._lock_ciclo(2)

    def test_o_lock_e_reentrante(self):
        """`reiniciar_camera` chama `parar_camera` e `iniciar_camera` já com o lock na
        mão — um Lock simples daria deadlock consigo mesmo."""
        lock = pipeline._lock_ciclo(CAM)
        with lock:
            with lock:                  # não pode travar
                pass
