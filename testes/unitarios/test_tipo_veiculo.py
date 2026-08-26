"""Filtro de tipo de veículo (moto/carro) no histórico de leituras.

O valor é uma ESTIMATIVA — a classe do detector de veículo (YOLOX, classes COCO) que vem
carregada na própria bbox da placa —, não um cadastro. O que estes testes protegem não é a
acurácia dela: é que o "não sei" continue distinguível do "é carro". Fundir os dois faria o
histórico afirmar, sobre centenas de leituras, um tipo que ninguém mediu.

Até 20/08/2026 a fonte era `e_moto` do AutoOCR (`header and aspect <= 2.0`), aposentada
porque o aspecto do bbox mede a folga do detector e não a diagramação da placa: 32,8% das
774 detecções reais caíam abaixo do limiar, e o mesmo veículo trocava de veredito com 2 px
de diferença (ver `TestMesmoVeiculoNaoTrocaDeVeredito`).
"""
from __future__ import annotations

import numpy as np
import pytest

from app.core import banco
from app.core.banco._deteccoes import _filtro_tipo_veiculo
from app.visao.detector import (BBoxPlaca, BuscaEmTiles, DetectorDoisEstagios, OrigemTipo,
                                VehicleDetector, deslocar, origem_de_bbox, tipo_de_bbox)
from app.visao.pipeline import Pipeline


class TestFiltroSQL:
    def test_todos_e_none_nao_filtram(self):
        assert _filtro_tipo_veiculo(None) == ""
        assert _filtro_tipo_veiculo("todos") == ""

    def test_desconhecido_vira_is_null_sem_parametro(self):
        """'desconhecido' não pode virar `= ?`: em SQL, `tipo_veiculo = NULL` nunca é
        verdadeiro e o filtro devolveria zero linhas em vez das não estimadas."""
        frag = _filtro_tipo_veiculo("desconhecido")
        assert "IS NULL" in frag and "?" not in frag

    def test_moto_e_carro_usam_parametro(self):
        for t in ("moto", "carro"):
            assert "?" in _filtro_tipo_veiculo(t)

    def test_valor_invalido_levanta(self):
        """Espelha `_filtro_origem`: falhar alto, em vez de devolver o conjunto errado."""
        with pytest.raises(ValueError, match="tipo_veiculo"):
            _filtro_tipo_veiculo("caminhao")


class TestGravacaoEFiltro:
    def _gravar(self, ambiente):
        ids = {
            "moto": banco.registrar_deteccao("ABC1D23", "mercosul", 0.9, tipo_veiculo="moto"),
            "carro": banco.registrar_deteccao("XYZ9K88", "mercosul", 0.9, tipo_veiculo="carro"),
            "nulo": banco.registrar_deteccao("HPY2371", "antigo", 0.9),
        }
        return ids

    def test_grava_e_devolve_o_tipo(self, ambiente):
        self._gravar(ambiente)
        por_placa = {d["placa"]: d for d in banco.listar_deteccoes(limit=50)}
        assert por_placa["ABC1D23"]["tipo_veiculo"] == "moto"
        assert por_placa["XYZ9K88"]["tipo_veiculo"] == "carro"
        assert por_placa["HPY2371"]["tipo_veiculo"] is None

    def test_omitir_o_tipo_grava_null_e_nao_carro(self, ambiente):
        """Default 'carro' seria inventar dado: leitura por engine único não estima."""
        banco.registrar_deteccao("HPY2371", "antigo", 0.9)
        assert banco.listar_deteccoes(limit=1)[0]["tipo_veiculo"] is None

    @pytest.mark.parametrize("tipo,esperadas", [
        ("moto", {"ABC1D23"}),
        ("carro", {"XYZ9K88"}),
        ("desconhecido", {"HPY2371"}),
        ("todos", {"ABC1D23", "XYZ9K88", "HPY2371"}),
        (None, {"ABC1D23", "XYZ9K88", "HPY2371"}),
    ])
    def test_cada_filtro_traz_o_seu_conjunto(self, ambiente, tipo, esperadas):
        self._gravar(ambiente)
        achadas = {d["placa"] for d in banco.listar_deteccoes(limit=50, tipo_veiculo=tipo)}
        assert achadas == esperadas

    def test_linhas_sem_estimativa_nao_somem_de_todos(self, ambiente):
        """Regressão do padrão que já mordeu o filtro de origem: um NOT IN faria as
        linhas NULL desaparecerem de TODOS os filtros, inclusive do "Todos"."""
        self._gravar(ambiente)
        todas = banco.listar_deteccoes(limit=50, tipo_veiculo="todos")
        assert any(d["tipo_veiculo"] is None for d in todas)

    def test_tipo_invalido_na_gravacao_levanta(self, ambiente):
        with pytest.raises(ValueError, match="tipo_veiculo"):
            banco.registrar_deteccao("ABC1D23", "mercosul", 0.9, tipo_veiculo="caminhao")

    def test_contagem_por_placa_respeita_o_filtro(self, ambiente):
        """O total do cabeçalho e a lista precisam concordar — senão a mesma tela
        mostra dois números que se contradizem."""
        banco.registrar_deteccao("ABC1D23", "mercosul", 0.9, tipo_veiculo="moto")
        banco.registrar_deteccao("ABC1D23", "mercosul", 0.9, tipo_veiculo="carro")
        assert banco.contar_deteccoes_placa("ABC1D23") == 2
        assert banco.contar_deteccoes_placa("ABC1D23", tipo_veiculo="moto") == 1
        assert banco.contar_deteccoes_placa("ABC1D23", tipo_veiculo="desconhecido") == 0

    def test_mesclagem_nao_apaga_o_tipo_ja_gravado(self, ambiente):
        """`atualizar_deteccao` roda quando o roteador repete a chamada do mesmo
        veículo. A leitura que mescla pode não ter estimativa; sobrescrever com NULL
        apagaria a que já estava lá."""
        id_ = banco.registrar_deteccao("ABC1D23", "mercosul", 0.9, tipo_veiculo="moto")
        banco.atualizar_deteccao(id_, placa="ABC1D23", padrao="mercosul", confianca=0.95)
        assert banco.listar_deteccoes(limit=1)[0]["tipo_veiculo"] == "moto"

    def test_mesclagem_preenche_o_tipo_que_faltava(self, ambiente):
        """`tipo_veiculo_fonte` é o DISCRIMINANTE do bloco: sem ele, `atualizar_deteccao`
        preserva os quatro campos como estavam, mesmo que `tipo_veiculo` venha preenchido
        — é o que impede uma leitura sem sinal cru de sobrescrever o veredito sozinho e
        deixar a linha inconsistente (ver `TestSinalCruDoTipoVeiculo`)."""
        id_ = banco.registrar_deteccao("ABC1D23", "mercosul", 0.9)
        banco.atualizar_deteccao(id_, placa="ABC1D23", padrao="mercosul", confianca=0.95,
                                 tipo_veiculo="moto", tipo_veiculo_fonte="veiculo")
        assert banco.listar_deteccoes(limit=1)[0]["tipo_veiculo"] == "moto"


class TestSinalCruDoTipoVeiculo:
    """`veiculo_classe`/`veiculo_conf`/`tipo_veiculo_fonte` — o sinal CRU por trás do
    veredito de `tipo_veiculo`. Mesmo precedente de `TestAcordoEConfirmacao`
    (test_banco.py): medida crua + veredito congelado.

    O que importa proteger aqui não é o valor em si — é que os QUATRO campos se movam
    sempre em BLOCO. Um COALESCE independente por coluna deixaria linha incoerente:
    `tipo_veiculo` sobrevivendo da leitura anterior enquanto `veiculo_classe` vem da
    nova — tipo de um veículo com sinal cru de outro.
    """

    def _linha(self, id_):
        import sqlite3
        from app.core.banco import _base
        con = sqlite3.connect(_base.caminho())
        con.row_factory = sqlite3.Row
        try:
            return dict(con.execute("SELECT * FROM deteccoes WHERE id=?", (id_,)).fetchone())
        finally:
            con.close()

    def test_grava_os_quatro_campos(self, ambiente):
        id_ = banco.registrar_deteccao("ABC1D23", "mercosul", 0.9, tipo_veiculo="moto",
                                       veiculo_classe=3, veiculo_conf=0.62,
                                       tipo_veiculo_fonte="veiculo")
        linha = self._linha(id_)
        assert linha["tipo_veiculo"] == "moto"
        assert linha["veiculo_classe"] == 3
        assert linha["veiculo_conf"] == 0.62
        assert linha["tipo_veiculo_fonte"] == "veiculo"

    def test_tipo_nulo_pode_ter_fonte_preenchida(self, ambiente):
        """O motivo do NULL é dado por si só — 'sem-veiculo' não afirma tipo nenhum,
        só explica por que não há um."""
        id_ = banco.registrar_deteccao("ABC1D23", "mercosul", 0.9,
                                       tipo_veiculo_fonte="sem-veiculo")
        linha = self._linha(id_)
        assert linha["tipo_veiculo"] is None
        assert linha["tipo_veiculo_fonte"] == "sem-veiculo"

    def test_fonte_invalida_levanta(self, ambiente):
        with pytest.raises(ValueError, match="tipo_veiculo_fonte"):
            banco.registrar_deteccao("ABC1D23", "mercosul", 0.9, tipo_veiculo_fonte="chute")

    def test_prefixo_replay_e_aceito(self, ambiente):
        """Vocabulário que CRESCE: `testes/recalcula_tipo_veiculo.py` marca o que
        reconstruiu com o prefixo `replay:`, para nunca se confundir com medida ao vivo."""
        id_ = banco.registrar_deteccao("ABC1D23", "mercosul", 0.9, tipo_veiculo="carro",
                                       tipo_veiculo_fonte="replay:veiculo")
        assert self._linha(id_)["tipo_veiculo_fonte"] == "replay:veiculo"

    def test_mesclar_sem_estimativa_preserva_o_bloco_anterior_inteiro(self, ambiente):
        """A leitura que mescla pode não ter estimativa (2 estágios desligado nesta
        chamada) — sobrescrever com NULL apagaria os quatro campos que já estavam lá."""
        id_ = banco.registrar_deteccao("ABC1D23", "mercosul", 0.9, tipo_veiculo="moto",
                                       veiculo_classe=3, veiculo_conf=0.7,
                                       tipo_veiculo_fonte="veiculo")
        banco.atualizar_deteccao(id_, placa="ABC1D23", padrao="mercosul", confianca=0.95)
        linha = self._linha(id_)
        assert linha["tipo_veiculo"] == "moto"
        assert linha["veiculo_classe"] == 3
        assert linha["veiculo_conf"] == 0.7
        assert linha["tipo_veiculo_fonte"] == "veiculo"

    def test_mesclar_com_estimativa_nova_troca_o_bloco_inteiro(self, ambiente):
        """O teste que pega o COALESCE independente voltando: se `tipo_veiculo` trocasse
        mas `veiculo_classe` ficasse do registro antigo, a linha gravaria 'carro' com a
        classe COCO de uma moto (3) — inconsistência silenciosa."""
        id_ = banco.registrar_deteccao("ABC1D23", "mercosul", 0.9, tipo_veiculo="moto",
                                       veiculo_classe=3, veiculo_conf=0.7,
                                       tipo_veiculo_fonte="veiculo")
        banco.atualizar_deteccao(id_, placa="ABC1D23", padrao="mercosul", confianca=0.95,
                                 tipo_veiculo="carro", veiculo_classe=2, veiculo_conf=0.88,
                                 tipo_veiculo_fonte="veiculo")
        linha = self._linha(id_)
        assert linha["tipo_veiculo"] == "carro"
        assert linha["veiculo_classe"] == 2
        assert linha["veiculo_conf"] == 0.88
        assert linha["tipo_veiculo_fonte"] == "veiculo"


class TestMapaClasseCoco:
    """A tradução de classe COCO do detector de veículo para 'moto'/'carro'/None.

    Substitui um teste que reimplementava `"moto" if e_moto else ...` na própria asserção
    — ele não importava `app.visao.ocr.auto` e passava mesmo com a regra de produção
    apagada, que é como a categorização quebrada atravessou 27 testes verdes.
    """

    @pytest.mark.parametrize("classe_coco,esperado", [
        (3, "moto"),      # motorcycle
        (2, "carro"),     # car
        (5, "carro"),     # bus  — a pergunta no posto é duas rodas vs quatro
        (7, "carro"),     # truck (o YOLOX chama picape assim)
    ])
    def test_mapa(self, classe_coco, esperado):
        assert VehicleDetector.TIPO_POR_CLASSE.get(classe_coco) == esperado

    def test_classe_fora_do_mapa_da_none_e_nao_estoura(self):
        """`veiculo_classes` é configurável: alguém pode incluir 1 (bicycle). Isso tem que
        virar "não estimado", não uma exceção dentro do laço de detecção."""
        assert VehicleDetector.TIPO_POR_CLASSE.get(1) is None
        assert VehicleDetector.TIPO_POR_CLASSE.get(0) is None


class TestBBoxPlaca:
    """O carrier que leva a ORIGEM do tipo (tipo + classe + confiança + fonte) junto
    com a bbox.

    A razão de ser uma subclasse de `tuple` de tamanho 5 é compatibilidade: todo consumidor
    desempacota cinco posicionalmente e alguns comparam com tupla crua por igualdade.
    """

    ORIGEM_MOTO = OrigemTipo(fonte="veiculo", tipo="moto", classe=3, conf=0.9)
    ORIGEM_CARRO = OrigemTipo(fonte="veiculo", tipo="carro", classe=2, conf=0.85)

    def test_e_indistinguivel_de_uma_tupla_de_cinco(self):
        bb = BBoxPlaca(10, 20, 100, 40, 0.9, self.ORIGEM_MOTO)
        assert len(bb) == 5
        assert bb == (10, 20, 100, 40, 0.9)
        assert [bb] == [(10, 20, 100, 40, 0.9)]   # é o que test_busca_em_tiles.py faz
        x, y, w, h, conf = bb
        assert (x, y, w, h, conf) == (10, 20, 100, 40, 0.9)

    def test_tipo_de_bbox_aceita_tupla_crua(self):
        """Tupla crua é entrada legítima — detector de 1 estágio, janelas, dublês. A
        ausência do atributo é a causa `sem-2-estagios`, não um `None` mudo."""
        assert tipo_de_bbox(BBoxPlaca(0, 0, 1, 1, 0.5, self.ORIGEM_CARRO)) == "carro"
        assert tipo_de_bbox(BBoxPlaca(0, 0, 1, 1, 0.5)) is None
        assert tipo_de_bbox((0, 0, 1, 1, 0.5)) is None
        assert origem_de_bbox((0, 0, 1, 1, 0.5)).fonte == "sem-2-estagios"

    def test_deslocar_preserva_a_origem_inteira(self):
        """O recorte por ROI translada as bboxes em dois caminhos, e a comprehension crua
        que existia nos dois — `[(x + rx, ...) for ...]` — derrubava a origem em silêncio.
        Preserva os QUATRO campos, não só o tipo."""
        bb = deslocar(BBoxPlaca(10, 20, 100, 40, 0.9, self.ORIGEM_MOTO), 5, 7)
        assert bb == (15, 27, 100, 40, 0.9)
        origem = origem_de_bbox(bb)
        assert origem.tipo == "moto"
        assert origem.classe == 3
        assert origem.conf == 0.9
        assert origem.fonte == "veiculo"

    def test_reconstruir_a_tupla_degrada_para_nao_estimado(self):
        """O modo de falha documentado: perde-se a origem, nunca se inventa um tipo."""
        bb = BBoxPlaca(10, 20, 100, 40, 0.9, self.ORIGEM_MOTO)
        assert tipo_de_bbox(tuple(bb)) is None
        assert origem_de_bbox(tuple(bb)).fonte == "sem-2-estagios"


class _VeiculoFalso:
    """Substitui `VehicleDetector` — sem carregar ONNX. Devolve 6-tuplas com a classe."""

    def __init__(self, veiculos):
        self._veiculos = veiculos
        self.sess = object()

    def carregar(self):
        pass

    def detectar(self, frame):
        return self._veiculos


class _PlacaFalsa:
    """Substitui o detector de placa. Devolve a bbox relativa ao recorte recebido."""

    def __init__(self, *retornos):
        self._retornos = list(retornos)
        self._n = 0
        self.sess = object()

    def carregar(self):
        pass

    def detectar(self, crop):
        r = self._retornos[min(self._n, len(self._retornos) - 1)]
        self._n += 1
        return list(r)


def _frame(w=800, h=600):
    return np.zeros((h, w, 3), dtype=np.uint8)


class _CapturaFalsa:
    """Substitui `CapturaDataset` — não escreve arquivo nenhum."""

    def amostrar(self, frame):
        pass

    def negativo(self, crop):
        pass


class TestDoisEstagiosAnexaClasse:
    """`DetectorDoisEstagios` é quem SABE de qual veículo cada placa saiu — a associação é
    estrutural (a placa foi achada dentro daquele recorte), não uma contenção geométrica
    redescoberta depois, que erraria com veículos sobrepostos."""

    @pytest.mark.parametrize("classe_coco,esperado", [
        (3, "moto"), (2, "carro"), (5, "carro"), (7, "carro"), (1, None),
    ])
    def test_placa_herda_o_tipo_do_veiculo_que_a_contem(self, classe_coco, esperado):
        det = DetectorDoisEstagios(
            _PlacaFalsa([(10, 10, 60, 28, 0.8)]),
            _VeiculoFalso([(100, 100, 300, 200, 0.72, classe_coco)]),
        )
        placas = det.detectar(_frame())
        assert len(placas) == 1
        origem = origem_de_bbox(placas[0])
        assert origem.tipo == esperado
        # O SINAL CRU vai junto, não só o veredito — classe COCO e confiança do veículo
        # (não confundir com a confiança da PLACA, `placas[0][4]`).
        assert origem.classe == classe_coco
        assert origem.conf == 0.72
        assert origem.fonte == ("veiculo" if esperado is not None else "classe-nao-mapeada")

    def test_sem_veiculo_o_fallback_nao_inventa_tipo(self):
        """Fallback no quadro inteiro: não houve veículo, logo não há classe a afirmar.
        `fonte='sem-veiculo'`, e não a ausência muda do atributo — são causas diferentes
        de NULL: aqui o estágio de veículo RODOU e não achou nada."""
        det = DetectorDoisEstagios(_PlacaFalsa([(10, 10, 60, 28, 0.8)]), _VeiculoFalso([]))
        placas = det.detectar(_frame())
        assert len(placas) == 1
        origem = origem_de_bbox(placas[0])
        assert origem.tipo is None
        assert origem.fonte == "sem-veiculo"

    def test_obrigatorio_sem_veiculo_nao_devolve_placa(self):
        det = DetectorDoisEstagios(_PlacaFalsa([(10, 10, 60, 28, 0.8)]),
                                   _VeiculoFalso([]), obrigatorio=True)
        assert det.detectar(_frame()) == []

    def test_dedup_preserva_a_origem_do_sobrevivente(self):
        """`_dedup` só filtra e ordena — se algum dia remontar a tupla, a origem cai. Dois
        veículos sobrepostos vendo a MESMA placa: sobra a de maior confiança, com tipo."""
        det = DetectorDoisEstagios(
            _PlacaFalsa([(10, 10, 60, 28, 0.7)], [(10, 10, 60, 28, 0.95)]),
            _VeiculoFalso([(100, 100, 300, 200, 0.9, 3),
                           (100, 100, 300, 200, 0.8, 3)]),
        )
        placas = det.detectar(_frame())
        assert len(placas) == 1
        assert placas[0][4] == 0.95
        assert origem_de_bbox(placas[0]).tipo == "moto"


class TestBuscaEmTilesNaoInventaTipo:
    """As janelas rodam SÓ o estágio de placa (ver o docstring de `BuscaEmTiles`): repetir
    o estágio de veículo por janela multiplicaria a latência sem ganho. `tiles` é causa
    DIFERENTE de `sem-veiculo` — aqui o estágio de veículo nem chegou a rodar."""

    class _DetectorPlacaVazio:
        sess = object()

        def carregar(self):
            pass

        def detectar(self, frame):
            return []

    class _DetectorTilesUmaVez:
        """Devolve uma bbox na primeira janela chamada; nada nas demais."""
        sess = object()

        def __init__(self, retorno):
            self._retorno = retorno
            self._usado = False

        def carregar(self):
            pass

        def detectar(self, tile):
            if self._usado:
                return []
            self._usado = True
            return [self._retorno]

    def test_placa_recuperada_por_janela_fica_sem_tipo_mas_com_fonte_tiles(self):
        busca = BuscaEmTiles(self._DetectorPlacaVazio(),
                             self._DetectorTilesUmaVez((5, 5, 20, 10, 0.6)))
        achados = busca.detectar(np.zeros((397, 610, 3), dtype=np.uint8))

        assert len(achados) == 1
        origem = origem_de_bbox(achados[0])
        assert origem.tipo is None
        assert origem.fonte == "tiles"

    def test_caminho_normal_repassa_a_origem_intacta(self):
        """Quando a passada única já achou (via 2 estágios), `BuscaEmTiles` não remonta
        a bbox — a origem chega intacta, com o tipo que o 2 estágios calculou."""
        origem = OrigemTipo.de_classe(2, 0.8)
        bb_pronta = BBoxPlaca(1, 2, 3, 4, 0.9, origem)

        class _DetectorComAcerto:
            sess = object()

            def carregar(self):
                pass

            def detectar(self, frame):
                return [bb_pronta]

        busca = BuscaEmTiles(_DetectorComAcerto(), self._DetectorPlacaVazio())
        achados = busca.detectar(np.zeros((100, 100, 3), dtype=np.uint8))
        assert origem_de_bbox(achados[0]) is origem


class TestMesmoVeiculoNaoTrocaDeVeredito:
    """A regressão que dá nome ao bug.

    Caso real, placa NPX9F15, mesma câmera, 19/08/2026, 3 minutos de diferença:

        deteccoes.id 743   bbox 59×27   aspecto 2,185  → gravou 'carro'
        deteccoes.id 746   bbox 56×28   aspecto 2,000  → gravou 'moto'

    Mesmo veículo, mesma posição, e nos dois snapshots a MESMA placa de carro (uma linha,
    sete caracteres). O veredito virava porque 2,000 passa no `<= 2.0` e 2,185 não — dois
    pixels de altura decidindo o tipo do veículo. Com a classe do veículo (COCO 2 = car)
    os dois recortes dão 'carro'.
    """

    def test_dois_recortes_do_mesmo_carro_dao_carro_nos_dois(self):
        det = DetectorDoisEstagios(
            _PlacaFalsa([(10, 10, 59, 27, 0.8)],    # aspecto 2,185 — antes 'carro'
                        [(10, 10, 56, 28, 0.8)]),   # aspecto 2,000 — antes 'moto'
            _VeiculoFalso([(100, 100, 300, 200, 0.9, 2)]),
        )
        frame = _frame()
        tipos = [tipo_de_bbox(det.detectar(frame)[0]) for _ in range(2)]
        assert tipos == ["carro", "carro"]


class TestPropagacaoPeloPipelineContinuo:
    """`Pipeline._emitir` (origem="pipeline") tem que gravar o `tipo_veiculo` que RECEBEU,
    igual à leitura reativa.

    O caminho contínuo produziu 100% dos rótulos que estavam no banco, então é aqui que a
    troca de fonte precisa valer. Antes, `_emitir` lia `self.ocr._ultimo_tipo_veiculo` —
    um atributo compartilhado, avaliado fora do escopo do crop: num quadro com dois
    veículos, ou num tick em que o consenso fechou sem OCR novo, gravava o tipo do veículo
    errado. Agora o valor desce por parâmetro desde a bbox que o gerou."""

    FRAME = np.zeros((10, 10, 3), dtype=np.uint8)

    def _pipeline(self, camera_db_id: int = 1):
        """`Pipeline.__new__` bypassa `__init__` — mesmo padrão de
        test_consenso_pipeline.py/test_pipeline_loop.py. Preenche só o que `_emitir`
        toca; nada de câmera/detector/OCR de verdade."""
        p = Pipeline.__new__(Pipeline)
        p.camera_db_id = camera_db_id
        p.cfg = {"camera_tipo": "rtsp"}
        p.cooldown_seg = 0
        p.salvar_snapshot = False
        p.salvar_frame = False
        p.roi = None
        p.bomba = 0
        p.lado = 0
        return p

    @pytest.mark.parametrize("estimativa", ["moto", "carro", None])
    def test_emitir_grava_os_quatro_campos_que_recebeu(self, ambiente, estimativa):
        p = self._pipeline()
        p.ocr = object()   # o OCR não participa mais desta decisão
        origem = (OrigemTipo(fonte="veiculo", tipo=estimativa,
                             classe=3 if estimativa == "moto" else 2, conf=0.8)
                  if estimativa else None)

        p._emitir("ABC1D23", "mercosul", 0.9, self.FRAME, (0, 0, 5, 5),
                  acordo=1.0, confirmada=True, votos=1, total_leituras=1,
                  origem_tipo=origem)

        gravada = banco.listar_deteccoes(limit=1)[0]
        assert gravada["placa"] == "ABC1D23"
        assert gravada["tipo_veiculo"] == estimativa
        # Os quatro campos vêm juntos — não só o veredito.
        assert gravada["veiculo_classe"] == (origem.classe if origem else None)
        assert gravada["veiculo_conf"] == (origem.conf if origem else None)
        assert gravada["tipo_veiculo_fonte"] == (origem.fonte if origem else None)

    def test_emitir_sem_o_argumento_grava_nulo_e_nao_quebra(self, ambiente):
        """Ausência é estado legítimo e frequente (2 estágios desligado, nenhum veículo
        detectado, placa vinda das janelas), por isso o parâmetro tem default. O que não
        pode é derrubar a emissão nem virar 'carro' por omissão."""
        p = self._pipeline()
        p.ocr = object()

        p._emitir("XYZ9K88", "mercosul", 0.9, self.FRAME, (0, 0, 5, 5),
                  acordo=1.0, confirmada=True, votos=1, total_leituras=1)

        gravada = banco.listar_deteccoes(limit=1)[0]
        assert gravada["tipo_veiculo"] is None
        assert gravada["veiculo_classe"] is None
        assert gravada["veiculo_conf"] is None
        assert gravada["tipo_veiculo_fonte"] is None

    def test_classico_leva_a_origem_da_bbox_ate_a_gravacao(self, ambiente):
        """`_processar_classico` → `_tentar_emitir` → `_emitir`: o caminho inteiro, não só
        a última função. É o que pega um "esqueci de passar" no meio da cadeia."""
        p = self._pipeline()
        p.frames_consenso = 1
        p.acordo_min = 0.0
        p.votos_minimos = 1
        p._historico = __import__("collections").deque(maxlen=10)
        p.tracker = None
        p.captura_dataset = _CapturaFalsa()

        class _OcrFalso:
            def ler(self, crop):
                return "ABC1D23", 0.9

        p.ocr = _OcrFalso()
        frame = np.zeros((200, 400, 3), dtype=np.uint8)
        origem = OrigemTipo(fonte="veiculo", tipo="moto", classe=3, conf=0.77)
        bboxes = [BBoxPlaca(10, 10, 60, 28, 0.8, origem)]

        p._processar_classico(frame, bboxes, 200, 400, frame.copy())

        gravada = banco.listar_deteccoes(limit=1)[0]
        assert gravada["placa"] == "ABC1D23"
        assert gravada["tipo_veiculo"] == "moto"
        assert gravada["veiculo_classe"] == 3
        assert gravada["veiculo_conf"] == 0.77
        assert gravada["tipo_veiculo_fonte"] == "veiculo"

    def test_tracker_casa_a_bbox_do_track_por_iou_e_leva_a_origem(self, ambiente):
        """`_processar_com_tracker` não recebe a origem pronta — recupera pela caixa de
        maior IoU entre as detecções do frame (`_origem_do_track`). É o caminho que gerou
        100% dos rótulos que estavam no banco antes desta correção."""
        from app.visao.pipeline import _origem_do_track

        origem = OrigemTipo(fonte="veiculo", tipo="carro", classe=2, conf=0.9)
        bboxes = [BBoxPlaca(10, 10, 60, 28, 0.8, origem)]

        # Caixa do track idêntica à da detecção (o caso comum do `_IoUTracker`, o backend
        # em uso quando boxmot não está instalado — casamento exato, IoU 1.0).
        assert _origem_do_track((10, 10, 60, 28), bboxes) is origem

        # Track sem detecção correspondente neste frame: nunca um chute, e também nunca
        # None — ver `TestTrackSemDeteccaoTemCausaPropria` para o porquê do rótulo próprio.
        sem_match = _origem_do_track((500, 500, 10, 10), bboxes)
        assert sem_match.tipo is None
        assert sem_match.fonte == "track-sem-deteccao"


class TestOCRNaoEstimaMaisTipoDeVeiculo:
    """O OCR saiu dessa decisão — mas SÓ dela.

    A aposentadoria tinha que remover `_ultimo_tipo_veiculo` sem levar de arrasto o
    `e_moto`/`formato_hint`, que é outra coisa: estratégia de leitura (qual engine roda
    primeiro, se o Paddle sobrepõe, qual hint vai ao `validar()`), calibrada em amostra
    medida e deliberadamente NÃO alterada nesta correção.
    """

    class _EngineFalso:
        """Substitui `app.visao.ocr.engines.OCR` — sem carregar nenhum modelo real."""
        engine = "tesseract"

        def __init__(self, placa_lida, tinha_header, e_mercosul_header):
            self._placa_lida = placa_lida
            self._tinha_header = tinha_header
            self._e_mercosul_header = e_mercosul_header

        def ler(self, crop):
            return (self._placa_lida or "", 0.9)

        def _remover_header(self, crop_bgr):
            return crop_bgr, self._tinha_header, self._e_mercosul_header

    def _multiocr(self, placa_lida, tinha_header, e_mercosul_header):
        from app.visao.ocr.auto import MultiOCR

        m = MultiOCR.__new__(MultiOCR)
        m._ocrs = [self._EngineFalso(placa_lida, tinha_header, e_mercosul_header)]
        m._ultimo_detalhe = {}
        return m

    # Os dois recortes que o limiar antigo classificava de formas opostas. Nenhum dos dois
    # pode mais produzir `tipo_veiculo` — nem no ramo vazio, nem no ramo com placa eleita.
    @pytest.mark.parametrize("aspecto_wh,placa", [
        ((100, 50), None),         # aspecto 2,0 — o antigo 'moto'
        ((400, 100), None),        # aspecto 4,0 — o antigo 'carro'
        ((100, 50), "ABC1D23"),
        ((400, 100), "ABC1D23"),
    ])
    def test_nao_expoe_tipo_nem_no_atributo_nem_no_dict(self, aspecto_wh, placa):
        w, h = aspecto_wh
        crop = np.zeros((h, w, 3), dtype=np.uint8)
        m = self._multiocr(placa_lida=placa, tinha_header=True, e_mercosul_header=True)

        det = m.ler_detalhado(crop)

        assert det["placa"] == placa
        assert "tipo_veiculo" not in det
        assert not hasattr(m, "_ultimo_tipo_veiculo")

    def test_mas_continua_calculando_o_hint_de_formato(self):
        """O `formato_hint` guiado pelo header Mercosul continua chegando ao `validar()`.

        Espera o hint FRACO ('mercosul'). O forte ('mercosul_moto') foi removido dos dois
        caminhos de OCR em 25/08/2026: ele reescrevia caractere sem ver a confiança por
        caractere, e o detector de faixa que o alimentava errou nos dois sentidos nas duas
        motos reais medidas. Este teste afirmava que o MultiOCR pedia o hint forte "e é ele
        que corrige posição de caractere (FBI0123 → FBI0I23)" — as duas metades deixaram de
        ser verdade, e a segunda nunca foi: ver
        `test_validador.TestAlternativasDeLinha` e o docstring de `validador._validar_7`,
        onde está medido que o hint fraco não muda resultado nenhum."""
        capturados = []
        m = self._multiocr(placa_lida="FBI0123", tinha_header=True, e_mercosul_header=True)

        import app.visao.validador as validador_mod
        real = validador_mod.validar
        try:
            def espiao(texto, hint=""):
                capturados.append(hint)
                return real(texto, hint)
            validador_mod.validar = espiao
            m.ler_detalhado(np.zeros((50, 100, 3), dtype=np.uint8))
        finally:
            validador_mod.validar = real

        assert capturados and all(h == "mercosul" for h in capturados)


class TestAutoOCRNaoCarregaEstadoDoCropAnterior:
    """Regressão do commit `dbca997`: os `return _sem_leitura()` novos saíam antes de
    calcular `_ultimo_e_moto`/`_ultimo_formato_hint`, e `__init__` não os inicializava. O
    atributo ficava com o valor do crop ANTERIOR — e 38,5% dos recortes caem nesse caminho.
    Como o `AutoOCRPaddle` arbitra a leitura com esses atributos, era bug de OCR, não só
    de relatório."""

    def _autoocr(self):
        from app.visao.ocr.auto import AutoOCR
        a = AutoOCR.__new__(AutoOCR)
        a.engine = "auto"
        a._ultimo_detalhe = {}
        a._ultimo_e_moto = False
        a._ultimo_formato_hint = ""
        return a

    def test_crop_descartado_zera_o_palpite_anterior(self):
        a = self._autoocr()
        # Estado que "sobrou" de um crop anterior classificado como moto Mercosul.
        a._ultimo_e_moto = True
        a._ultimo_formato_hint = "mercosul_moto"

        # Recorte abaixo do mínimo do OCR: sai por `crop_legivel`, sem rodar engine.
        a.ler_detalhado(np.zeros((3, 4, 3), dtype=np.uint8))

        assert a._ultimo_e_moto is False
        assert a._ultimo_formato_hint == ""

    def test_crop_nulo_tambem_zera(self):
        a = self._autoocr()
        a._ultimo_e_moto = True
        a._ultimo_formato_hint = "mercosul_moto"

        a.ler_detalhado(None)

        assert a._ultimo_e_moto is False
        assert a._ultimo_formato_hint == ""


class TestAmbiguidadeEntreVeiculosSobrepostos:
    """A mesma placa achada dentro de DOIS veículos de classes diferentes.

    Acontece de verdade num posto: moto no bico com um carro atrás, caixas se cruzando no
    plano da imagem, e o recorte do carro (com 5% de padding) contendo a placa da moto. O
    `_dedup` mantinha "a de maior confiança de PLACA" — uma medida que nada diz sobre de
    quem é o veículo —, então dava para gravar a placa da moto como 'carro' com
    `fonte='veiculo'`: um erro AFIRMATIVO, pior que NULL.
    """

    def test_classes_diferentes_viram_ambiguo_em_vez_de_escolher(self):
        det = DetectorDoisEstagios(
            # mesma placa nos dois recortes; a do carro sai com confiança MAIOR
            _PlacaFalsa([(10, 10, 60, 28, 0.62)], [(10, 10, 60, 28, 0.71)]),
            _VeiculoFalso([(100, 100, 200, 200, 0.9, 3),     # moto
                           (100, 100, 400, 300, 0.8, 2)]),   # carro atrás
        )
        placas = det.detectar(_frame())

        assert len(placas) == 1
        origem = origem_de_bbox(placas[0])
        assert origem.tipo is None, "não pode afirmar tipo quando os dois veículos discordam"
        assert origem.fonte == "veiculo-ambiguo"

    def test_classes_iguais_mantem_o_tipo(self):
        """Dois carros sobrepostos concordam — aqui o tipo continua valendo."""
        det = DetectorDoisEstagios(
            _PlacaFalsa([(10, 10, 60, 28, 0.62)], [(10, 10, 60, 28, 0.71)]),
            _VeiculoFalso([(100, 100, 200, 200, 0.9, 2),
                           (100, 100, 400, 300, 0.8, 7)]),   # car + truck: ambos 'carro'
        )
        placas = det.detectar(_frame())
        assert len(placas) == 1
        assert tipo_de_bbox(placas[0]) == "carro"


class TestTrackSemDeteccaoTemCausaPropria:
    """`_origem_do_track` devolvia None quando o track vinha sem detecção nova no quadro
    (ByteTrack prevendo por Kalman durante oclusão). Isso gravava `tipo_veiculo_fonte=NULL`,
    que o esquema reserva para linha ANTERIOR À MIGRAÇÃO — a causa ficava indistinguível de
    "leitura antiga", exatamente a confusão que a coluna existe para eliminar."""

    def test_sem_casamento_devolve_causa_nomeada_e_nao_none(self):
        from app.visao.pipeline import _origem_do_track

        # caixa de track longe de qualquer detecção deste quadro
        origem = _origem_do_track((900, 900, 50, 25), [BBoxPlaca(10, 10, 50, 25, 0.9, None)])

        assert origem is not None
        assert origem.tipo is None
        assert origem.fonte == "track-sem-deteccao"

    def test_com_casamento_usa_a_origem_da_deteccao(self):
        from app.visao.detector import OrigemTipo
        from app.visao.pipeline import _origem_do_track

        alvo = BBoxPlaca(10, 10, 50, 25, 0.9, OrigemTipo.de_classe(3, 0.44))
        origem = _origem_do_track((10, 10, 50, 25), [alvo])

        assert origem.tipo == "moto"
        assert origem.fonte == "veiculo"
        assert origem.conf == 0.44
