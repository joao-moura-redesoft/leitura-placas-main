"""Formato do retorno de `ler_placa` — o contrato que o roteador do posto consome.

Existe como rede de segurança para refatorações do laço de leitura. O consumidor é um
sidecar Java de outro time (docs/INTEGRACAO_ROTEADOR.md): renomear ou sumir com uma
chave daqui não quebra teste nenhum de lógica, não aparece em revisão de diff do lado
deles, e só falha em produção — no meio de um abastecimento.

Não mede acurácia (isso é o harness em `testes/`, não a suíte unitária): detector e OCR
são substituídos por dublês determinísticos, e o que se verifica é o ENVELOPE — quais
chaves saem, com que tipo, e nos dois desfechos possíveis (leu / não leu).

O bloco `veiculo` (dados da apiplacas) NÃO sai daqui: ele é acrescentado por
`app/web/leitura.py`, a jusante — ver `test_nao_consulta_a_api_externa` abaixo.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.visao import leitura as leitura_mod
from app.visao.detector import BBoxPlaca, OrigemTipo

# Chaves do retorno com placa lida (app/visao/leitura.py, fim de `_ler_placa`).
# `fontes`/`avisos`/`n_cameras_votando` são aditivos (bico de 2 câmeras); o restante é o
# contrato que o sidecar Java já consome e não pode mudar de significado.
CHAVES_COM_PLACA = {
    "camera_id", "bico_id", "placa", "padrao", "confianca", "votos_snapshot",
    "total_snapshots", "votos_ocr", "total_engines", "detalhes_ocr", "snapshot",
    "frame_url", "tentativas", "acordo", "confirmada", "parada_motivo", "tipo_veiculo",
    "n_cameras_votando", "fontes", "avisos",
    # `votos_leitura` entrou em 25/08/2026 AO LADO de `votos_snapshot`, nunca no lugar
    # dele: os dois contam coisas diferentes e os dois importam.
    #   votos_snapshot -> FOTOS desta chamada que bateram com a placa (contrato publicado
    #                     em docs/INTEGRACAO_ROTEADOR.md, o sidecar Java já lê)
    #   votos_leitura  -> LEITURAS que apoiam a placa. Com o ensemble, uma foto rende 3-4
    #                     leituras de modelos diferentes, e é este número que decide
    #                     `confirmada` — "2 fotos" era inalcançável com o GET conseguindo
    #                     1 foto em 28 s, e por isso NADA era confirmado.
    "votos_leitura",
}

# Chaves do retorno sem placa. Conjunto DIFERENTE de propósito: sem leitura não há
# consenso nem crop, e `bboxes_detectadas` separa "não vi placa" de "vi e não li".
CHAVES_SEM_PLACA = {
    "placa", "mensagem", "frame_url", "camera_id", "bico_id", "bboxes_detectadas",
    "snapshots_analisados", "tentativas", "parada_motivo", "fontes", "avisos",
}

BICO_ID, CAMERA_ID = 1, 7
PLACA = "ABC1D23"

CFG = {
    "deteccao_automatica": "sim",
    "snapshots_votacao": "3",
    "leitura_max_tentativas": "6",
    # 120 s, e nao 10: o que este arquivo mede e "o laco para por CONSENSO antes de gastar
    # o orcamento de TENTATIVAS", e esse orcamento e `leitura_max_tentativas`, que nao
    # depende de relogio. Com 10 s o teto virava relogio de parede: as tres primeiras
    # rodadas do laco (`snapshots_votacao`) levam milissegundos com a maquina livre, mas
    # numa maquina ocupada podem passar de 10 s, e ai `parada_motivo` vem "timeout" e as
    # assercoes de contrato caem sem nada de errado no codigo de producao.
    #
    # Observado em 25/08/2026: cinco testes deste arquivo falharam duas vezes, as duas com o
    # servidor do posto VIVO em paralelo (2 cameras em deteccao continua). Nao consegui
    # reproduzir com carga controlada depois que o servidor parou, entao a causa nao esta
    # PROVADA - mas a dependencia de relogio esta no codigo (`leitura.py`: `if time.time() -
    # inicio > timeout_seg`) e nao serve a nada que este arquivo queira medir. Nenhum teste
    # daqui exercita o caminho de timeout: o unico que o menciona so aceita qualquer um dos
    # tres motivos de parada.
    "leitura_timeout_seg": "120",
    "leitura_acordo_minimo": "0.80",
    "cooldown_seg": "120",
    "salvar_frame_deteccao": "nao",
    "salvar_snapshot": "nao",
}


# Classe COCO 2 = car; a origem inclui a classe/confiança crua que a bbox carrega junto
# com o veredito — ver `app/visao/detector.py:OrigemTipo`.
_ORIGEM_CARRO = OrigemTipo(fonte="veiculo", tipo="carro", classe=2, conf=0.87)


class _DetectorFalso:
    """Devolve sempre a mesma caixa — ou nenhuma, para o caminho "não detectou".

    A caixa é uma `BBoxPlaca` porque `tipo_veiculo` viaja NELA, e não no OCR: é o detector
    de veículo que classifica moto/carro. Um dublê devolvendo tupla crua também é entrada
    válida (detector de 1 estágio) e faria o payload sair com `tipo_veiculo: None`.
    """

    def __init__(self, com_bbox: bool = True):
        self.com_bbox = com_bbox

    def detectar(self, frame):
        return [BBoxPlaca(100, 100, 120, 40, 0.9, _ORIGEM_CARRO)] if self.com_bbox else []


class _OcrFalso:
    """`ler_detalhado` é o caminho preferido do laço (`hasattr(ocr, 'ler_detalhado')`)."""

    def __init__(self, placa: str | None = PLACA):
        self.placa = placa

    def ler_detalhado(self, crop):
        if not self.placa:
            return {"placa": None, "padrao": None, "confianca": 0.0,
                    "votos": 0, "total_engines": 1, "detalhes": []}
        return {
            "placa": self.placa, "padrao": "mercosul", "confianca": 0.93,
            "votos": 1, "total_engines": 1,
            "detalhes": [{"engine": "falso", "placa": self.placa,
                          "padrao": "mercosul", "confianca": 0.93}],
        }


@pytest.fixture
def visao_falsa(monkeypatch, tmp_path):
    """Substitui detector/OCR pelos dublês e tira as escritas de disco do repositório.

    Sobrepõe o `_sem_visao` do conftest (que faz os carregadores explodirem de
    propósito): aqui o laço PRECISA rodar, só que sem modelo de verdade.
    """
    import app.visao.detector as det
    import app.visao.ocr as ocr

    monkeypatch.setattr(leitura_mod, "SNAPSHOT_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(leitura_mod, "PREVIEW_DIR", tmp_path / "privados")

    def instalar(detector, ocr_inst):
        monkeypatch.setattr(det, "obter_detector_leitura", lambda _cfg: detector)
        monkeypatch.setattr(ocr, "obter_ocr_leitura", lambda _cfg: ocr_inst)

    return instalar


def _provedor_de_frames():
    """Frame novo a cada chamada — o laço trata `is` para não votar duas vezes no mesmo."""
    def _obter():
        return np.zeros((480, 640, 3), dtype=np.uint8)
    return _obter


def _especificacao():
    """Basta `camera_tipo`: com frame vindo do pipeline nada mais é consultado, mas a
    gravação da detecção lê o tipo para a coluna legada `deteccoes.camera_id`."""
    return leitura_mod.EspecificacaoCamera.de_camera_db({"camera_tipo": "rtsp"}, CFG)


def _fonte(camera_id=CAMERA_ID, papel="traseira", roi=None):
    return leitura_mod.FonteLeitura(
        camera_id=camera_id, papel=papel, especificacao=_especificacao(), roi=roi,
        provider=_provedor_de_frames())


def _ler(**kw):
    base = dict(fontes=[_fonte()], cfg=CFG,
                preview_nome=f"preview_bico_{BICO_ID}", bico_id=BICO_ID, origem="teste")
    return leitura_mod.ler_placa(**{**base, **kw})


class TestPayloadComPlaca:
    def test_chaves_exatas(self, ambiente, visao_falsa):
        visao_falsa(_DetectorFalso(), _OcrFalso())
        r = _ler()
        assert set(r) == CHAVES_COM_PLACA

    def test_nao_consulta_a_api_externa(self, ambiente, visao_falsa):
        """O enriquecimento com dados do veículo (apiplacas, consulta PAGA) mora na camada
        web, não aqui — e este teste existe para que continue assim.

        `ler_placa` tem um segundo chamador: `bicos_ler_placa_teste`, que é o botão
        "Testar como o roteador" e o editor de ROI, clicados em rajada ao ajustar
        enquadramento. Mover o gancho para dentro desta função faria cada clique custar
        crédito pré-pago. Além disso, aqui a placa eleita ainda pode mudar
        (`_mesclar_com_historico`), e consultar antes disso gravaria cache sob uma placa
        que a própria função descarta.
        """
        visao_falsa(_DetectorFalso(), _OcrFalso())
        r = _ler()
        assert "veiculo" not in r

    def test_valores_do_contrato(self, ambiente, visao_falsa):
        """Os campos que o roteador de fato consulta para decidir se cobra a placa."""
        visao_falsa(_DetectorFalso(), _OcrFalso())
        r = _ler()
        assert r["placa"] == PLACA
        assert r["padrao"] == "mercosul"
        assert r["camera_id"] == CAMERA_ID
        assert r["bico_id"] == BICO_ID
        assert r["frame_url"] == f"/api/bicos/{BICO_ID}/preview.jpg"
        assert isinstance(r["confirmada"], bool)
        assert 0.0 <= r["acordo"] <= 1.0
        assert r["tentativas"] >= 1
        assert r["parada_motivo"] in ("acordo", "timeout", "max_tentativas")
        # Vem da bbox do detector, não do OCR — o dublê de OCR nem tem o campo. É o que
        # prova a troca de fonte no caminho que o roteador consome.
        assert r["tipo_veiculo"] == "carro"

    def test_para_por_consenso_antes_do_maximo(self, ambiente, visao_falsa):
        """Reject-retry: com todas as fotos concordando, o laço não gasta o orçamento
        inteiro — é o que segura a resposta dentro da tolerância do roteador."""
        visao_falsa(_DetectorFalso(), _OcrFalso())
        r = _ler()
        assert r["parada_motivo"] == "acordo"
        assert r["tentativas"] < int(CFG["leitura_max_tentativas"])

    def test_grava_o_preview_no_caminho_que_a_rota_serve(self, ambiente, visao_falsa):
        """`frame_url` só vale se o arquivo existir onde a rota autenticada procura."""
        visao_falsa(_DetectorFalso(), _OcrFalso())
        _ler()
        assert leitura_mod.caminho_preview_bico(BICO_ID).exists()


class TestPayloadSemPlaca:
    def test_chaves_exatas_quando_o_detector_nao_ve_nada(self, ambiente, visao_falsa):
        visao_falsa(_DetectorFalso(com_bbox=False), _OcrFalso())
        r = _ler()
        assert set(r) == CHAVES_SEM_PLACA
        assert r["placa"] is None
        assert r["bboxes_detectadas"] == 0

    def test_distingue_detectou_mas_nao_leu(self, ambiente, visao_falsa):
        """Os dois desfechos se resolvem de formas OPOSTAS (enquadramento vs zoom), e
        `bboxes_detectadas` é o único campo que os separa."""
        visao_falsa(_DetectorFalso(com_bbox=True), _OcrFalso(placa=None))
        r = _ler()
        assert r["placa"] is None
        assert r["bboxes_detectadas"] > 0
        assert "recorte" in r["mensagem"]
