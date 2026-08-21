"""Seleção automática de engine e ensembles (AutoOCR, AutoOCRPaddle, MultiOCR)."""
from __future__ import annotations
import logging
import threading

import cv2
import numpy as np

from app.visao import contexto_log
from app.visao.ocr.engines import OCR

log = logging.getLogger(__name__)


def _realcar_para_ocr(crop, alvo_h: int = 224, limiar_blur: float = 3500.0):
    """Amplia + afia o crop APENAS quando ele está borrado (placa distante/baixa-res).

    O gatilho é a NITIDEZ do crop (variância do Laplaciano), não o tamanho: crop nítido
    (lapvar alto) passa intacto — sharpen nele criaria artefatos e PIORARIA o OCR. Crop
    borrado (lapvar baixo) é ampliado por interpolação cúbica e afiado, recuperando as
    bordas dos caracteres.

    Calibrado em placas reais (UFPR-ALPR) vs sintéticas nítidas:
      - nítidas: lapvar ≥ 4554  → não mexe
      - borradas reais: lapvar mediano ~1400 (p90 ~3200) → realça
    Efeito no OCR de placas borradas reais: 48% → 60% (+12pp), sem regressão nas nítidas.
    """
    if crop is None or crop.size == 0 or crop.ndim != 3:
        return crop
    cinza = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    if cv2.Laplacian(cinza, cv2.CV_64F).var() >= limiar_blur:
        return crop  # já nítido — não mexe
    h = crop.shape[0]
    if h < alvo_h:
        f = alvo_h / max(h, 1)
        crop = cv2.resize(crop, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
    # Unsharp mask SUAVE (amount 0.6) — recupera nitidez sem os artefatos de um kernel
    # forte, que criavam confusões de caractere (O→D) em placas já limítrofes.
    borrado = cv2.GaussianBlur(crop, (0, 0), sigmaX=1.0)
    return cv2.addWeighted(crop, 1.6, borrado, -0.6, 0)


def _fmt(engine: str, bruto: str, validado) -> str:
    """`easyocr:''→—` / `fast_plate_ocr:'1CD4J18'→ICD4J18` — o que cada engine contribuiu.

    O par bruto→validado numa peça só porque a diferença entre os dois É a informação:
    `'11D4318'→IID4318` mostra o validador trocando 1 por I, e é isso que explica placas
    quase-iguais votando separado no tracker.
    """
    return "%s:%r→%s" % (engine, bruto, validado[0] if validado else "—")


# Abaixo disto o recorte não tem pixel para conter sete caracteres, e o que sai do OCR é
# ruído com um número de confiança em cima: gasta ~600 ms de EasyOCR + fast_plate_ocr,
# entra como voto no tracker e vai para a fila de classificação, tudo indistinguível de
# leitura boa.
#
# MEDIDO em 13/08/2026 sobre as capturas reais em `app/web/static/snapshots/`: 936
# recortes que o contínuo não conseguiu ler e 526 que produziram leitura "válida".
#
#     corte     barra dos 936 não-lidos    custa das 526 leituras
#     10x5          116 (11,9%)                 1 (0,2%)
#     20x8          328 (33,6%)                 4 (0,8%)
#     24x10         376 (38,5%)                 4 (0,8%)   ← escolhido
#     30x10         695 (71,1%)                 7 (1,3%)
#     40x12         811 (83,0%)                22 (4,2%)
#
# 24x10 é o joelho: barra 38,5% do desperdício e as 4 leituras que ele custa são
# 3x2 px (MJG6G66), 11x11 (BAC5276), 13x6 (PAO6S01) e 17x8 (SUJ7I11) — no máximo 2,4 px
# por caractere, ou seja, alucinação que estava sendo GRAVADA como detecção. O custo real
# é zero leitura verdadeira. Para comparação, a mediana de quem lê de verdade é 58x26 e o
# p10 é 51x18: o corte fica bem longe da faixa que funciona.
#
# NÃO fui além de 24x10 de propósito. 30x10 ainda pareceria barato (7 leituras), mas as 3
# a mais — 25x17, 26x18, 27x21 — estão em 3,6 px/caractere: provavelmente erradas, e
# "provavelmente" não é o padrão que este arquivo usa para descartar leitura (ver a
# arbitragem do AutoOCRPaddle e o limiar de `e_moto`). Refazer a conta quando a fila de
# classificação tiver rótulo humano nessa faixa.
CROP_MIN_LARGURA = 24
CROP_MIN_ALTURA = 10


def _sem_leitura() -> dict:
    """Resultado de "não li nada", na forma que `ler`/`ler_detalhado` prometem.

    `total_engines: 0` distingue este caso de "rodei os dois engines e nenhum validou"
    (que devolve 2) — quem lê `_ultimo_detalhe` consegue separar recorte descartado de
    leitura tentada e falha.
    """
    return {"placa": None, "padrao": None, "confianca": 0.0,
            "votos": 0, "total_engines": 0, "detalhes": []}


def crop_legivel(w: int, h: int) -> bool:
    """Vale sobre o recorte COMO VEIO do detector — antes de `_realcar_para_ocr`.

    Ampliar não cria informação: um crop de 4x3 px interpolado para 224 px de altura
    continua sem os caracteres, mas passaria em qualquer checagem feita depois. Os
    limiares acima foram medidos em recortes crus, e é neles que precisam ser aplicados.
    """
    return w >= CROP_MIN_LARGURA and h >= CROP_MIN_ALTURA


class AutoOCR:
    """Seleciona o engine automaticamente pelo formato e tipo da placa:

    - Mercosul carro  (header + aspect > 2.0) → fast_plate_ocr
      (ViT treinado em linha única — ótimo para carro)
    - Mercosul moto   (header + aspect ≤ 2.0) → easyocr
      (2 linhas de texto — fast_plate_ocr confunde layout de moto)
    - Antigo          (sem header)            → easyocr

    Se o engine preferido não produzir leitura válida, usa o outro como fallback.
    Interface compatível com OCR e MultiOCR (.carregar(), .ler(), .ler_detalhado()).
    """

    def __init__(self, tesseract_psm: int = 7,
                 deskew_ativo: bool = True, deskew_angulo_max: float = 30.0):
        self._fast = OCR(engine="fast_plate_ocr", tesseract_psm=tesseract_psm,
                         deskew_ativo=deskew_ativo, deskew_angulo_max=deskew_angulo_max)
        self._easy = OCR(engine="easyocr", tesseract_psm=tesseract_psm,
                         deskew_ativo=deskew_ativo, deskew_angulo_max=deskew_angulo_max)
        self.engine = "auto"
        self._ultimo_detalhe: dict = {}
        # Inicializados aqui porque `AutoOCRPaddle` os lê por `getattr` para arbitrar
        # (prioridade do Paddle, hint passado a validar()). Sem isto eles nascem
        # inexistentes e, depois da primeira leitura, ficam permanentemente com o valor do
        # último crop que chegou até o cálculo — inclusive quando o crop ATUAL foi
        # descartado antes disso.
        self._ultimo_e_moto = False
        self._ultimo_formato_hint = ""

    def carregar(self) -> None:
        self._fast.carregar()
        self._easy.carregar()

    def ler(self, crop) -> tuple[str, float]:
        det = self.ler_detalhado(crop)
        self._ultimo_detalhe = det
        return det["placa"] or "", det["confianca"]

    def ler_detalhado(self, crop) -> dict:
        from app.visao.validador import validar

        # Zera o palpite do crop ANTERIOR antes dos descartes abaixo. Sem isto, um recorte
        # rejeitado por tamanho (38,5% deles, medido) deixava `_ultimo_e_moto` e
        # `_ultimo_formato_hint` valendo do crop passado, e o `AutoOCRPaddle` arbitrava a
        # leitura seguinte com estado de outro veículo.
        self._ultimo_e_moto = False
        self._ultimo_formato_hint = ""

        # Antes de qualquer coisa: recorte sem pixel para sete caracteres não vai a
        # engine nenhum. Medido ANTES do realce de propósito — ver `crop_legivel`.
        if crop is None or crop.ndim != 3 or crop.size == 0:
            log.info("OCR sem recorte utilizável — nenhum engine rodado")
            return _sem_leitura()
        h0, w0 = crop.shape[:2]
        if not crop_legivel(w0, h0):
            log.info("OCR crop=%dx%dpx DESCARTADO — abaixo de %dx%d, sem pixel para "
                     "7 caracteres (nenhum engine rodado)",
                     w0, h0, CROP_MIN_LARGURA, CROP_MIN_ALTURA)
            return _sem_leitura()

        # Realce (upscale + sharpen) — recupera placas pequenas/borradas antes do OCR.
        crop = _realcar_para_ocr(crop)

        tinha_header = False
        e_mercosul_header = False
        if crop is not None and crop.ndim == 3 and crop.size > 0:
            _, tinha_header, e_mercosul_header = self._fast._remover_header(crop)

        # Moto: aspect ≤ 2 (200×140 vs 400×130) — 2 linhas de texto, easyocr é superior
        #
        # MEDIDO em 12/08/2026 nas 28 fotos reais de `testes/dataset.json`: o limiar 2,0
        # cai NO MEIO da faixa dos carros, não entre as classes.
        #
        #     moto  (n=2)   aspect 1,14 e 1,17
        #     carro (n=26)  aspect 1,45 .. 3,47   ← sete abaixo de 2,0
        #
        # Três carros (aspect 1,45/1,63/1,64) são classificados como moto, e o efeito não
        # se limita ao hint logo abaixo: `e_moto` troca o engine PRINCIPAL de
        # fast_plate_ocr para easyocr (linha ~91) e, no AutoOCRPaddle, dá prioridade ao
        # PaddleOCR. Os três erraram a leitura.
        #
        # NÃO ajustado de propósito. Nestes dados algo perto de 1,3 separaria as classes,
        # mas são DUAS motos — calibrar limiar com essa amostra é o mesmo erro que este
        # arquivo já documenta na arbitragem do AutoOCRPaddle. Refazer a medição quando o
        # dataset tiver ~10 motos reais; a fila de classificação em /testes é o caminho.
        aspect = (crop.shape[1] / max(crop.shape[0], 1)) if crop is not None else 3.0
        e_moto = tinha_header and aspect <= 2.0
        self._ultimo_e_moto = e_moto

        # APOSENTADO em 20/08/2026: `e_moto` NÃO alimenta mais `deteccoes.tipo_veiculo`.
        # Ele decide só estratégia de OCR daqui para baixo. A coluna passou a vir da
        # classe do detector de veículo (`DetectorDoisEstagios`, em app/visao/detector.py),
        # que carrega o tipo na própria bbox.
        #
        # Medido nas 774 detecções reais do banco: 12 dos 25 rótulos gravados eram 'moto'
        # — num posto de combustível —, e 11 deles foram refutados rodando o YOLOX nos
        # quadros salvos. A placa NPX9F15 chegou a receber vereditos opostos no mesmo
        # veículo com 3 min de diferença (bbox 59×27 → 'carro', 56×28 → 'moto'), porque
        # 2,000 passa no `<= 2.0` e 2,185 não. Com 32,8% da população abaixo do limiar, a
        # regra não separava classes: separava ruído de enquadramento.
        #
        # Não foi trocada por um limiar melhor de propósito. O aspecto do bbox mede a
        # FOLGA do detector, não a diagramação da placa: a mesma Mercosul de carro que
        # tem 3,08 no papel chega com 2,0 a 60 px de largura. Não há limiar que conserte.

        # fast_plate_ocr como principal para carros (com cabeçalho, Mercosul ou antigo com tarjeta)
        # Não dependemos da cor (e_mercosul_header) aqui para garantir que funcione de noite (câmeras IR)
        if tinha_header and not e_moto:
            principal, fallback = self._fast, self._easy   # Carro
        else:
            principal, fallback = self._easy, self._fast   # Moto (layout quadrado, easyocr é melhor)

        # Quando header Mercosul confirmado, passa hint para validar(). Moto usa um hint
        # mais forte ("mercosul_moto") porque o layout 2-linhas (aspecto do crop) já
        # confirma o formato de forma confiável — não depende só da cor do header, então
        # pode corrigir com prioridade (ex: FBI0123 → FBI0I23). Carro usa o hint mais fraco
        # ("mercosul", só cor) que NUNCA corrompe um match antigo direto e limpo — evita
        # que um falso-positivo do detector de header (ex: cartão de teste colorido)
        # corrompa uma leitura antigo correta (ex: CDV2112 → CDV2I12).
        #
        # MEDIDO em 12/08/2026 (28 fotos reais): a defesa acima FUNCIONA, mas o detector
        # de header acerta pouco nos dois sentidos — 4 das 10 antigas recebem hint de
        # Mercosul, e 15 das 18 Mercosul não recebem hint nenhum. Ou seja, este caminho
        # quase nunca dispara em produção. Nas 3 fotos em que o hint saiu errado,
        # `validar(lido, '')` e `validar(lido, 'mercosul_moto')` deram resultado IDÊNTICO:
        # o hint errado não corrompeu leitura nenhuma. A suspeita registrada até aqui — de
        # que o falso positivo do header corromperia placa antiga — não se confirmou.
        if tinha_header and e_mercosul_header:
            formato_hint = "mercosul_moto" if e_moto else "mercosul"
        else:
            formato_hint = ""
        # Guardado para a subclasse AutoOCRPaddle validar a leitura do PaddleOCR com o
        # MESMO hint. Sem isso o Paddle era validado "cru" e perdia justamente as
        # correções de posição que o hint faz (FBI0123 → FBI0I23).
        self._ultimo_formato_hint = formato_hint

        # `layout` é o palpite sobre a DIAGRAMAÇÃO do recorte (quantas linhas, tem faixa
        # no topo), que é o que decide qual engine roda primeiro. Não confundir com o
        # `padrao` que sai do validador, que é o veredito sobre a placa lida — o log
        # antigo chamava os dois de "tipo" e exibia `tipo=antigo` seguido, linhas depois,
        # de `Placa detectada: ... (mercosul)`, parecendo contradição.
        layout = ("moto-mercosul" if e_moto else ("mercosul-carro" if e_mercosul_header else "antigo"))
        # `w0 x h0` é o recorte CRU; `crop` aqui já pode ter sido ampliado pelo realce, e
        # exibir o tamanho ampliado esconderia a única medida que diz se havia informação
        # para ler. O marcador ILEGIVEL que existia aqui saiu junto com a chegada do corte
        # em `crop_legivel`: o que não passa não chega mais nesta linha.
        cabecalho = "crop=%dx%dpx aspect=%.2f layout=%s" % (w0, h0, aspect, layout)

        texto, conf = principal.ler(crop)
        resultado = validar(texto, formato_hint)
        log.debug(
            "AutoOCR %s: bruto=%r → validado=%r conf=%.2f",
            principal.engine, texto, resultado[0] if resultado else None, conf,
        )
        detalhes = [{
            "engine": principal.engine,
            "placa": resultado[0] if resultado else None,
            "padrao": resultado[1] if resultado else None,
            "confianca": round(conf, 3),
        }]

        # Aceita sem tentar fallback apenas se: não é moto E confiança alta.
        # Moto tem 2 linhas de texto e erra mais — sempre compara os dois engines.
        # Confiança baixa (< 50%) também força comparação para evitar leituras erradas.
        if resultado and not e_moto and conf >= 0.50:
            log.info("OCR %s | %s → %s conf=%.2f (sem fallback)", cabecalho,
                     _fmt(principal.engine, texto, resultado), resultado[0], conf)
            return {
                "placa": resultado[0], "padrao": resultado[1],
                "confianca": round(conf, 3),
                "votos": 1, "total_engines": 1, "detalhes": detalhes,
            }

        motivo_fallback = "moto" if e_moto else ("conf_baixa=%.2f" % conf if resultado else "sem_resultado")
        log.debug("AutoOCR: rodando fallback=%s motivo=%s", fallback.engine, motivo_fallback)

        # Executa fallback: sempre para moto, ou quando principal falhou/conf baixa
        texto2, conf2 = fallback.ler(crop)
        resultado2 = validar(texto2, formato_hint)
        log.debug(
            "AutoOCR %s: bruto=%r → validado=%r conf=%.2f",
            fallback.engine, texto2, resultado2[0] if resultado2 else None, conf2,
        )
        engines_lidos = "%s | %s" % (_fmt(principal.engine, texto, resultado),
                                     _fmt(fallback.engine, texto2, resultado2))
        detalhes.append({
            "engine": fallback.engine,
            "placa": resultado2[0] if resultado2 else None,
            "padrao": resultado2[1] if resultado2 else None,
            "confianca": round(conf2, 3),
        })

        # Ambos validam: escolhe o vencedor.
        if resultado and resultado2:
            if e_moto:
                # Moto (2 linhas): o principal é a EasyOCR, confiável nesse layout.
                # O fast_plate_ocr é treinado em 1 linha e às vezes valida uma leitura
                # ERRADA com confiança alta — NÃO deve sobrepor a EasyOCR aqui.
                melhor, melhor_conf, vencedor = resultado, conf, principal.engine
            else:
                melhor = resultado2 if conf2 > conf else resultado
                melhor_conf = conf2 if conf2 > conf else conf
                vencedor = fallback.engine if conf2 > conf else principal.engine
            log.info("OCR %s | %s → %s conf=%.2f (ambos validaram, vence %s)",
                     cabecalho, engines_lidos, melhor[0], melhor_conf, vencedor)
            return {
                "placa": melhor[0], "padrao": melhor[1],
                "confianca": round(melhor_conf, 3),
                "votos": 1, "total_engines": 2, "detalhes": detalhes,
            }

        if resultado:
            log.info("OCR %s | %s → %s conf=%.2f (só %s validou)", cabecalho,
                     engines_lidos, resultado[0], conf, principal.engine)
            return {
                "placa": resultado[0], "padrao": resultado[1],
                "confianca": round(conf, 3),
                "votos": 1, "total_engines": 2, "detalhes": detalhes,
            }

        if resultado2:
            log.info("OCR %s | %s → %s conf=%.2f (só %s validou)", cabecalho,
                     engines_lidos, resultado2[0], conf2, fallback.engine)
            return {
                "placa": resultado2[0], "padrao": resultado2[1],
                "confianca": round(conf2, 3),
                "votos": 1, "total_engines": 2, "detalhes": detalhes,
            }

        log.info("OCR %s | %s → NADA (nenhum engine validou)", cabecalho, engines_lidos)
        return {
            "placa": None, "padrao": None, "confianca": 0.0,
            "votos": 0, "total_engines": 2, "detalhes": detalhes,
        }


class AutoOCRPaddle(AutoOCR):
    """AutoOCR + PaddleOCR como reforço.

    O PaddleOCR (PP-OCR, Apache-2.0) lê muito melhor placas antigas/borradas reais
    (UFPR-ALPR: ~70% vs ~50% do AutoOCR), mas é fraco no limpo.

    Carro — arbitragem:
      - PaddleOCR e AutoOCR concordam        → mantém.
      - AutoOCR não validou                  → usa PaddleOCR (se validar).
      - Discordam                            → decide pela NITIDEZ do crop:
            nítido (lapvar ≥ limiar) → AutoOCR; borrado (< limiar) → PaddleOCR.

    Moto — o PaddleOCR TEM PRIORIDADE. Até 12/08/2026 esta classe descartava a leitura
    do Paddle em moto, com a justificativa de que ele "não lê layout empilhado". Isso não
    era verdade sobre o Paddle: ele lê as duas linhas e devolve uma caixa para cada — era
    `OCR._ler_paddleocr` que ficava só com a maior e jogava metade da placa fora. Corrigido
    aquele defeito, a ordem se inverte, medido nas 27 motos de `testes/dataset.json`:

        PaddleOCR      22/27 (81,5%)
        fast_plate_ocr  2/27 ( 7,4%)   ← engine que o AutoOCR usa como fallback em moto

    Mantido o AutoOCR como segunda opinião: ele decide quando o Paddle não valida.

    Uso recomendado só na leitura GET (tolera a latência maior do PaddleOCR).
    """

    def __init__(self, tesseract_psm: int = 7, limiar_nitidez: float = 3500.0,
                 deskew_ativo: bool = True, deskew_angulo_max: float = 30.0):
        super().__init__(tesseract_psm, deskew_ativo=deskew_ativo,
                         deskew_angulo_max=deskew_angulo_max)
        self._paddle = OCR(engine="paddleocr", tesseract_psm=tesseract_psm,
                           deskew_ativo=deskew_ativo, deskew_angulo_max=deskew_angulo_max)
        self._limiar_nitidez = limiar_nitidez

    def carregar(self) -> None:
        super().carregar()
        self._paddle.carregar()

    def ler_detalhado(self, crop) -> dict:
        from app.visao.validador import validar

        if crop is None or crop.ndim != 3 or crop.size == 0:
            return super().ler_detalhado(crop)

        # Recorte degenerado sai aqui, e não só dentro do `super()`: o caminho de crop
        # nítido abaixo aciona o PaddleOCR justamente quando o AutoOCR não validou nada
        # — que é exatamente o que um recorte descartado devolve. Sem esta guarda, barrar
        # os dois engines do AutoOCR só empurraria o trabalho para o Paddle, que é o mais
        # caro dos três.
        h0, w0 = crop.shape[:2]
        if not crop_legivel(w0, h0):
            return super().ler_detalhado(crop)

        # Nitidez decide a estratégia ANTES de rodar qualquer engine (medida é barata:
        # só um Laplaciano). Cada engine sozinho custa segundos num crop pequeno/borrado
        # (medido: AutoOCR ~3s, PaddleOCR ~3s em CPU) — por isso a estratégia muda:
        cinza = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        nitidez = cv2.Laplacian(cinza, cv2.CV_64F).var()
        crop_nitido = nitidez >= self._limiar_nitidez

        if crop_nitido:
            # Nítido: AutoOCR sozinho já é confiável para CARRO (ver arbitragem abaixo, que
            # sempre manteria o AutoOCR aqui) — roda só ele. Aciona o Paddle quando o
            # AutoOCR não validou nada, ou quando é MOTO (aí o Paddle é o mais forte, ver
            # os números no docstring da classe).
            d = super().ler_detalhado(crop)
            e_moto = getattr(self, "_ultimo_e_moto", False)
            if not e_moto and d.get("placa") is not None:
                return d
            texto_p, conf_p = self._paddle.ler(crop)
            vp = validar(texto_p, getattr(self, "_ultimo_formato_hint", ""))
            if not vp:
                return d
            placa_p, padrao_p = vp
            if e_moto and d.get("placa") == placa_p:
                log.debug("Paddle confirma %s (crop nítido, lapvar=%.0f)", placa_p, nitidez)
                return d                      # concordam: nada a trocar
            # A troca precisa aparecer: sem esta linha o log mostrava o AutoOCR decidindo
            # uma placa e, adiante, o sistema emitindo OUTRA, sem nada no meio explicando
            # a substituição. Quem fosse investigar uma leitura errada deste caminho não
            # tinha como saber que o Paddle tinha entrado.
            log.info("Paddle SOBREPÕE %s → %s conf=%.2f (crop nítido lapvar=%.0f, motivo=%s)",
                     d.get("placa") or "NADA", placa_p, conf_p, nitidez,
                     "moto" if e_moto else "auto não validou")
            d = dict(d)
            d["placa"], d["padrao"], d["confianca"] = placa_p, padrao_p, round(conf_p, 3)
            d.setdefault("detalhes", []).append(
                {"engine": "paddleocr", "placa": placa_p, "padrao": padrao_p, "confianca": round(conf_p, 3)}
            )
            return d

        # Borrado: os dois PODEM legitimamente contribuir, então rodam EM PARALELO (thread)
        # em vez de sequencial — sequencial custaria a SOMA dos dois (~6s); paralelo custa
        # o MAIOR dos dois (~3s), já que numpy/onnxruntime liberam o GIL durante a inferência.
        # Vale para carro e para moto — em moto o Paddle é inclusive o mais forte dos dois.
        resultado: dict = {}
        # O rótulo [camN trkN] mora em threading.local e NÃO é herdado — sem repassá-lo,
        # tudo que o AutoOCR logar aqui dentro sai sem dono no meio do log das câmeras.
        ctx = contexto_log.capturar()

        def _rodar_auto() -> None:
            with contexto_log.herdar(ctx):
                resultado["d"] = AutoOCR.ler_detalhado(self, crop)

        t = threading.Thread(target=_rodar_auto, daemon=True)
        t.start()
        texto_p, conf_p = self._paddle.ler(crop)
        t.join()
        d = resultado["d"]

        vp = validar(texto_p, getattr(self, "_ultimo_formato_hint", ""))
        if not vp:
            return d
        placa_p, padrao_p = vp
        placa_a = d.get("placa")

        if placa_a == placa_p:
            log.debug("Paddle confirma %s (crop borrado, lapvar=%.0f)", placa_p, nitidez)
            return d  # concordam

        # Discordam. Crop borrado (nitidez < limiar) → Paddle; e em moto o Paddle também
        # tem prioridade, por ser o mais forte nesse layout. Nos dois casos o Paddle leva.
        #
        # O desempate ignora QUANTOS engines sustentam cada lado, e há um caso real medido
        # (testes/fotos/real_mercosul_carro_1.jpg) em que isso custa a leitura: easyocr e
        # fast_plate_ocr leem LSN4I49 (mercosul), o Paddle sozinho lê LSN4149, e o Paddle
        # vence só por o crop ser borrado (lapvar 93 contra limiar 3500). A validação não
        # tem como barrar: LSN4149 é uma placa ANTIGA válida, e o hint 'mercosul' de carro
        # é deliberadamente fraco (validador.py:90) para não corromper match antigo limpo.
        #
        # NÃO foi alterado: trocar o desempate para maioria de engines é plausível — o
        # projeto já vota em MultiOCR — mas hoje só há 5 fotos reais no dataset, e mudar
        # política de arbitragem com essa amostra é o erro que criou este bug. Medir
        # quando o dataset real crescer.
        votos_a = sum(1 for x in d.get("detalhes", []) if x.get("placa") == placa_a)
        log.info("Paddle SOBREPÕE %s → %s conf=%.2f (crop borrado lapvar=%.0f, %d engine(s) "
                 "sustentavam o anterior, moto=%s)",
                 placa_a or "NADA", placa_p, conf_p, nitidez, votos_a,
                 getattr(self, "_ultimo_e_moto", False))
        d = dict(d)
        d["placa"], d["padrao"], d["confianca"] = placa_p, padrao_p, round(conf_p, 3)
        d.setdefault("detalhes", []).append(
            {"engine": "paddleocr", "placa": placa_p, "padrao": padrao_p, "confianca": round(conf_p, 3)}
        )
        return d


class MultiOCR:
    """Executa múltiplos engines e elege o resultado por votação majoritária.

    Interface compatível com OCR (mesmos .carregar() e .ler()).
    Adiciona .ler_detalhado() que devolve votos e resultado por engine.
    """

    def __init__(self, engines: list[str], tesseract_psm: int = 7,
                 deskew_ativo: bool = True, deskew_angulo_max: float = 30.0):
        # Remove duplicatas preservando ordem; garante ao menos um engine
        vistos: set[str] = set()
        unicos = []
        for e in engines:
            if e and e not in vistos:
                vistos.add(e)
                unicos.append(e)
        if not unicos:
            unicos = ["tesseract"]
        self._ocrs = [OCR(engine=e, tesseract_psm=tesseract_psm,
                          deskew_ativo=deskew_ativo, deskew_angulo_max=deskew_angulo_max)
                      for e in unicos]
        self.engine = ",".join(unicos)  # compatibilidade com estado.ocr_engine_ativo
        self._ultimo_detalhe: dict = {}

    def carregar(self) -> None:
        for ocr in self._ocrs:
            ocr.carregar()

    def ler(self, crop) -> tuple[str, float]:
        det = self.ler_detalhado(crop)
        self._ultimo_detalhe = det
        return det["placa"] or "", det["confianca"]

    def ler_detalhado(self, crop) -> dict:
        """Roda todos os engines e retorna votação + detalhes individuais."""
        from collections import Counter
        from app.visao.validador import validar

        # Mesmo hint de formato que AutoOCR passa a validar() (ver auto.py:96-106) —
        # sem isso, MultiOCR perdia a correção posicional guiada pelo header Mercosul
        # (ex.: moto FBI0123 → FBI0I23) só por não estar no caminho `ocr_engine=auto`.
        #
        # `e_moto` aqui serve SÓ ao `formato_hint`, igual ao AutoOCR. Ele não alimenta
        # `deteccoes.tipo_veiculo`: essa coluna vem da classe do detector de veículo desde
        # 20/08/2026 (ver a justificativa medida em `AutoOCR.ler_detalhado`).
        formato_hint = ""
        e_moto = False
        if crop is not None and crop.ndim == 3 and crop.size > 0 and self._ocrs:
            _, tinha_header, e_mercosul_header = self._ocrs[0]._remover_header(crop)
            aspect = crop.shape[1] / max(crop.shape[0], 1)
            e_moto = tinha_header and aspect <= 2.0
            if tinha_header and e_mercosul_header:
                formato_hint = "mercosul_moto" if e_moto else "mercosul"

        detalhes = []
        for ocr in self._ocrs:
            texto_bruto, conf = ocr.ler(crop)
            resultado = validar(texto_bruto, formato_hint)
            detalhes.append({
                "engine": ocr.engine,
                "placa": resultado[0] if resultado else None,
                "padrao": resultado[1] if resultado else None,
                "confianca": round(conf, 3),
            })

        validos = [(d["placa"], d["confianca"]) for d in detalhes if d["placa"]]
        total = len(self._ocrs)

        if not validos:
            return {
                "placa": None, "padrao": None, "confianca": 0.0,
                "votos": 0, "total_engines": total, "detalhes": detalhes,
            }

        votos = Counter(p for p, _ in validos)
        placa, n_votos = votos.most_common(1)[0]
        confs = [c for p, c in validos if p == placa]
        padrao = next(d["padrao"] for d in detalhes if d["placa"] == placa)

        return {
            "placa": placa,
            "padrao": padrao,
            "confianca": round(sum(confs) / len(confs), 3),
            "votos": n_votos,
            "total_engines": total,
            "detalhes": detalhes,
        }


# OCR dedicado à leitura sob demanda (botão "Ler Placa"/GET) — cacheado.
_ocr_leitura = None
_ocr_leitura_id: tuple | None = None

# Protege chamadas concorrentes ao OCR cacheado acima — mesmo motivo do
# detector_leitura_lock em detector.py: duas leituras GET simultâneas (2+ câmeras)
# compartilhariam os mesmos engines (fast_plate_ocr/EasyOCR/PaddleOCR) de threads
# diferentes. Seguro em CPU, arriscado em GPU (CUDA). Serializa por segurança.
ocr_leitura_lock = threading.Lock()

# Protege a CRIAÇÃO do OCR cacheado acima (diferente do lock acima, que protege o USO) —
# mesmo motivo de `_detector_leitura_criacao_lock` em detector.py: sem isso, duas
# requisições concorrentes vendo `_ocr_leitura is None` ao mesmo tempo carregam a pilha
# de engines cada uma a sua própria vez.
_ocr_leitura_criacao_lock = threading.Lock()


def obter_ocr_leitura(cfg: dict):
    """OCR de alta acurácia para a leitura GET. Com ocr_engine=auto e ocr_leitura_paddle=sim,
    usa o ensemble AutoOCRPaddle (reforço PaddleOCR para placa borrada). Carregado uma vez.
    O stream ao vivo continua com o OCR mais leve do pipeline."""
    global _ocr_leitura, _ocr_leitura_id
    engine = cfg.get("ocr_engine", "auto")
    psm = int(cfg.get("tesseract_psm", "7"))
    usar_paddle = str(cfg.get("ocr_leitura_paddle", "sim")).strip().lower() in ("sim", "true", "1")
    extras = [e.strip() for e in cfg.get("ocr_engines_extra", "").split(",") if e.strip()]
    deskew_on = str(cfg.get("deskew_ativo", "sim")).strip().lower() in ("sim", "true", "1", "yes")
    deskew_max = float(cfg.get("deskew_angulo_max", "30"))
    ident = (engine, psm, usar_paddle, tuple(extras), deskew_on, deskew_max)

    if _ocr_leitura is None or _ocr_leitura_id != ident:
        with _ocr_leitura_criacao_lock:
            if _ocr_leitura is None or _ocr_leitura_id != ident:
                if engine == "auto" and usar_paddle:
                    novo = AutoOCRPaddle(tesseract_psm=psm,
                                         deskew_ativo=deskew_on, deskew_angulo_max=deskew_max)
                elif engine == "auto":
                    novo = AutoOCR(tesseract_psm=psm,
                                   deskew_ativo=deskew_on, deskew_angulo_max=deskew_max)
                elif extras:
                    novo = MultiOCR(engines=[engine] + extras, tesseract_psm=psm,
                                    deskew_ativo=deskew_on, deskew_angulo_max=deskew_max)
                else:
                    novo = OCR(engine=engine, tesseract_psm=psm,
                               deskew_ativo=deskew_on, deskew_angulo_max=deskew_max)
                novo.carregar()
                _ocr_leitura = novo
                _ocr_leitura_id = ident
                log.info("OCR de leitura (GET) carregado: engine=%s paddle=%s deskew=%s",
                         engine, usar_paddle, deskew_on)
    return _ocr_leitura
