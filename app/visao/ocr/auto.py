"""Seleção automática de engine e ensembles (AutoOCR, AutoOCRPaddle, MultiOCR)."""
from __future__ import annotations
import logging
import threading

import cv2

from app.visao import _fabrica_singleton as _fab
from app.visao.consenso import (
    agrupar_por_veiculo, consenso_caractere, leitura_real_proxima, peso_medio,
    prior_de_formato,
)
from app.visao.ocr.engines import FAST_MODELOS_DEFAULT, OCR

log = logging.getLogger(__name__)


def _leituras_do_engine(engine, crop) -> list[tuple[str, float]]:
    """Leituras de um engine, sem o chamador precisar saber se ele e um ensemble.

    `ler_varias` e o caminho novo (o fast_plate_ocr devolve uma leitura por modelo). O
    fallback para `ler()` existe para os dubles de teste, que implementam so o contrato
    antigo: um atributo novo que eles nao conhecem tem de degradar para uma leitura, nunca
    estourar `AttributeError` no meio da passada de OCR.

    FILTRA sem reconstruir. `[(t, c) for t, c in ...]` monta tuplas NOVAS e joga fora o
    `por_char` de cada `LeituraOCR` — era exatamente o que esta funcao fazia, e com isso a
    confianca por caractere NUNCA chegava a fusao: `getattr(leitura, "por_char", None)` em
    `_ler_com_engines` dava None sempre, e cada leitura votava com o escalar (a media) em
    todas as 7 posicoes. O mecanismo central do ensemble estava morto desde o commit que o
    criou (8f5dc4f, 26/08/2026); `engines.py:_ler_fast_varias` ja alertava sobre isto num
    comentario, e a armadilha foi cair aqui, uma camada acima. (Auditoria 27/08, achado K6.)

    Medido no recorte da RLX2A77: `BLX2677` sai com
    `[0.99, 0.30, 0.97, 0.99, 0.22, 0.99, 0.99]` — as posicoes 1 e 4 sao as duas erradas, e
    o modelo sabe disso. Sem o vetor, elas votam com o mesmo peso das certas, e os dois
    modelos `cct-*` (mesma familia, erro correlacionado) amplificam o proprio erro.
    """
    if engine is None:
        return []
    try:
        if hasattr(engine, "ler_varias"):
            return [l for l in engine.ler_varias(crop) if l[0]]
        texto, conf = engine.ler(crop)
        return [(texto, conf)] if texto else []
    except Exception as e:
        # Um engine que explode custa o VOTO dele, nunca a leitura. Qualquer `cv2.error` do
        # pre-processamento (imdecode devolvendo None, getPerspectiveTransform num
        # quadrilatero degenerado) derrubava a passada inteira, e o roteador levava 500 em
        # vez do payload degradado. Logado como ERROR aqui, no logger do app, porque no
        # caminho antigo a excecao ia para o threading.excepthook - stderr do uvicorn, fora
        # do log - e a causa real ficava invisivel.
        log.error("Engine de OCR falhou e foi ignorado nesta leitura: %s", e, exc_info=True)
        return []


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


# Aspecto máximo do recorte para tratá-lo como layout de MOTO (200×140 vs 400×130): duas
# linhas de texto, onde a easyocr é superior à fast_plate_ocr (treinada em linha única).
ASPECTO_MOTO_MAX = 2.0


def _layout_do_crop(crop, tinha_header: bool) -> tuple[float, bool]:
    """`(aspect, e_moto)` do recorte — a decisão de LAYOUT que guia a estratégia de OCR.

    Existe como função porque `AutoOCR` e `MultiOCR` calculavam isto separadamente, com o
    `2.0` escrito duas vezes. O limiar está marcado para recalibração (ver abaixo), e uma
    regra duplicada é uma regra que diverge na primeira vez que alguém ajusta só um lado —
    o mesmo motivo que fez `app/visao/consenso.py` existir.

    NÃO alimenta `deteccoes.tipo_veiculo`: essa coluna vem da classe do detector de veículo
    desde 20/08/2026. Aqui o valor decide só qual engine roda primeiro, se o PaddleOCR
    sobrepõe e qual `formato_hint` vai ao `validar()`.

    MEDIDO em 12/08/2026 nas 28 fotos reais de `testes/dataset.json`: o limiar 2,0 cai NO
    MEIO da faixa dos carros, não entre as classes.

        moto  (n=2)   aspect 1,14 e 1,17
        carro (n=26)  aspect 1,45 .. 3,47   ← sete abaixo de 2,0

    Três carros (aspect 1,45/1,63/1,64) rodam a estratégia de moto e erraram a leitura.
    NÃO ajustado de propósito: nestes dados algo perto de 1,3 separaria as classes, mas são
    DUAS motos, e calibrar com essa amostra é o erro que este arquivo já documenta na
    arbitragem do AutoOCRPaddle. Refazer quando o dataset tiver ~10 motos reais — a fila de
    classificação em /testes é o caminho.
    """
    aspect = (crop.shape[1] / max(crop.shape[0], 1)) if crop is not None else 3.0
    return aspect, bool(tinha_header and aspect <= ASPECTO_MOTO_MAX)


def crop_legivel(w: int, h: int) -> bool:
    """Vale sobre o recorte COMO VEIO do detector — antes de `_realcar_para_ocr`.

    Ampliar não cria informação: um crop de 4x3 px interpolado para 224 px de altura
    continua sem os caracteres, mas passaria em qualquer checagem feita depois. Os
    limiares acima foram medidos em recortes crus, e é neles que precisam ser aplicados.
    """
    return w >= CROP_MIN_LARGURA and h >= CROP_MIN_ALTURA


class AutoOCR:
    """Ensemble de OCR: TODOS os membros leem, e a fusao por caractere decide a placa.

    Nao ha mais "engine preferido" nem fallback. Ate 25/08/2026 esta classe escolhia um
    engine pelo layout do recorte (faixa colorida + aspecto) e usava o outro so se o
    primeiro falhasse, e essa escolha era o ponto onde a leitura certa morria: o detector de
    faixa errou nos dois sentidos nas duas motos medidas no bico 3 do ALTIPLANO, e o sistema
    chegou a ler `RLX2A77` a 0,96 com todos os char_probs >= 0,93 e emitir `HDX2477`.

    Agora todo membro le sempre e as leituras vao para `_fundir`, que vota posicao a posicao.
    Medido nas 30 fotos rotuladas (26 carro + 4 moto): 13/30 com um modelo escolhendo, 20/30
    com o ensemble fundindo.

    Interface compativel com OCR e MultiOCR (.carregar(), .ler(), .ler_detalhado()).
    """

    def __init__(self, tesseract_psm: int = 7,
                 deskew_ativo: bool = True, deskew_angulo_max: float = 30.0,
                 fast_modelos: tuple[str, ...] | None = None,
                 usar_easyocr: bool = False):
        self._fast = OCR(engine="fast_plate_ocr", tesseract_psm=tesseract_psm,
                         deskew_ativo=deskew_ativo, deskew_angulo_max=deskew_angulo_max,
                         fast_modelos=fast_modelos or FAST_MODELOS_DEFAULT)
        # EasyOCR entra so se pedida. Medida nas mesmas 30 fotos, ela contribui ZERO para a
        # fusao (17/26 em carro com e sem ela; 18/26 com e sem ela quando o Paddle esta no
        # pool) e acerta 1/26 sozinha, custando 212 ms por recorte - mais que os tres modelos
        # do fast juntos (62 ms). Era a PRINCIPAL do ramo sem-faixa, o que a punha na frente
        # justamente nas placas que este arquivo estava errando. Fica disponivel porque o
        # numero e de UM dataset de 30 fotos e quem operar pode querer remedir no posto.
        self._easy = None
        if usar_easyocr:
            self._easy = OCR(engine="easyocr", tesseract_psm=tesseract_psm,
                             deskew_ativo=deskew_ativo, deskew_angulo_max=deskew_angulo_max)
        self.engine = "auto"
        self._ultimo_detalhe: dict = {}
        # Inicializados aqui porque sao lidos por `getattr` fora do fluxo normal. Sem isto
        # eles nascem inexistentes e, depois da primeira leitura, ficam permanentemente com o
        # valor do ultimo crop que chegou ate o calculo - inclusive quando o crop ATUAL foi
        # descartado antes disso.
        self._ultimo_e_moto = False
        self._ultimo_formato_hint = ""

    def carregar(self) -> None:
        self._fast.carregar()
        if self._easy is not None:
            self._easy.carregar()

    def ler(self, crop) -> tuple[str, float]:
        det = self.ler_detalhado(crop)
        self._ultimo_detalhe = det
        return det["placa"] or "", det["confianca"]

    def _engines(self):
        """(nome, engine) de cada engine que contribui leitura, na ordem do log.

        Lista e nao atributos soltos porque o pool e PLANO: quem le nao tem "principal" e
        "fallback", tem membros. `AutoOCRPaddle` so acrescenta o seu a lista.
        """
        membros = [("fast_plate_ocr", self._fast)]
        if getattr(self, "_easy", None) is not None:
            membros.append(("easyocr", self._easy))
        return membros

    def ler_detalhado(self, crop) -> dict:
        # Zera o palpite do crop ANTERIOR antes dos descartes abaixo. Sem isto, um recorte
        # rejeitado por tamanho (38,5% deles, medido) deixava `_ultimo_e_moto` e
        # `_ultimo_formato_hint` valendo do crop passado, e a leitura seguinte era decidida
        # com estado de outro veiculo.
        self._ultimo_e_moto = False
        self._ultimo_formato_hint = ""

        # Antes de qualquer coisa: recorte sem pixel para sete caracteres nao vai a
        # engine nenhum. Medido ANTES do realce de proposito - ver `crop_legivel`.
        if crop is None or crop.ndim != 3 or crop.size == 0:
            log.info("OCR sem recorte utilizavel - nenhum engine rodado")
            return _sem_leitura()
        h0, w0 = crop.shape[:2]
        if not crop_legivel(w0, h0):
            log.info("OCR crop=%dx%dpx DESCARTADO - abaixo de %dx%d, sem pixel para "
                     "7 caracteres (nenhum engine rodado)",
                     w0, h0, CROP_MIN_LARGURA, CROP_MIN_ALTURA)
            return _sem_leitura()

        # Realce (upscale + sharpen) - recupera placas pequenas/borradas antes do OCR.
        crop = _realcar_para_ocr(crop)

        tinha_header = False
        e_mercosul_header = False
        if crop is not None and crop.ndim == 3 and crop.size > 0:
            # Mesmo motivo de `_leituras_do_engine`: isto e pre-processamento em cv2, e uma
            # excecao aqui derrubaria a leitura ANTES de qualquer engine rodar. Sem header
            # detectado o pior caso e ficar sem o hint de formato, que a fusao dispensa.
            try:
                _, tinha_header, e_mercosul_header = self._fast._remover_header(crop)
            except Exception as e:
                log.error("Deteccao de faixa falhou (%s) - seguindo sem hint de formato",
                          e, exc_info=True)

        aspect, e_moto = _layout_do_crop(crop, tinha_header)
        self._ultimo_e_moto = e_moto

        # `formato_hint` continua saindo da COR do header, e so na forma fraca ('mercosul'),
        # que nunca corrompe um match antigo direto e limpo.
        #
        # O hint forte 'mercosul_moto' foi REMOVIDO em 25/08/2026. Ele reescrevia caracteres
        # sem ver a confianca por caractere, e o detector de faixa - sua unica fonte - errou
        # nos DOIS sentidos nas duas motos medidas no bico 3 do ALTIPLANO: falso negativo na
        # Mercosul RLX2A77 (faixa azul visivel) e falso positivo na antiga metalica OSL2659.
        # No falso positivo, `validar('OSL2655', 'mercosul_moto')` trocou a posicao 4 - que o
        # modelo havia lido com 0,99 de confianca - e devolveu 'OSL2G55', transformando um
        # erro de 1 caractere em 2 e invertendo o padrao. Sem o hint sai 'OSL2655' e a fusao
        # conserta o resto.
        formato_hint = "mercosul" if (tinha_header and e_mercosul_header) else ""
        self._ultimo_formato_hint = formato_hint

        # `layout` e o palpite sobre a DIAGRAMACAO do recorte (quantas linhas, tem faixa no
        # topo). Nao confundir com o `padrao` que sai do validador, que e o veredito sobre a
        # placa lida. Hoje o layout so aparece no log: ele NAO escolhe mais engine nem hint,
        # porque com pool plano nao ha escolha a fazer - todos os membros leem sempre e a
        # fusao decide. Foi essa escolha que, ao errar, matava a leitura boa.
        layout = ("moto-mercosul" if e_moto else ("mercosul-carro" if e_mercosul_header else "antigo"))
        cabecalho = "crop=%dx%dpx aspect=%.2f layout=%s" % (w0, h0, aspect, layout)

        brutas = []
        for nome, eng in self._engines():
            for leitura in _leituras_do_engine(eng, crop):
                # Nao desempacota `for texto, conf in ...`: isso descartaria o `por_char` da
                # `LeituraOCR` (ver o docstring daquela classe). O 4o elemento e a confianca
                # POR POSICAO, ou None no engine que nao expoe.
                brutas.append((nome, leitura[0], leitura[1],
                               getattr(leitura, "por_char", None)))
        return self._fundir(brutas, formato_hint, cabecalho)

    def _fundir(self, brutas, formato_hint: str, cabecalho: str) -> dict:
        """Funde as leituras CRUAS de todos os membros numa placa, votando por posicao.

        Substitui a arbitragem que existia aqui (quem e principal, "aceita sem fallback se
        conf >= 0,50", "em moto o principal vence", "o Paddle sobrepoe"). Toda aquela cadeia
        servia para ELEGER uma string, e era onde as leituras boas morriam: em 24/08/2026 o
        sistema leu `RLX2A77` a 0,96 com todos os char_probs >= 0,93 e emitiu `HDX2477`,
        vinda de outra passada com dois caracteres abaixo de 0,62.

        Funde os textos CRUS e valida DEPOIS, nesta ordem. Validar antes aplica correcao
        posicional em cada leitura isolada, e duas correcoes erradas em posicoes diferentes
        se somam no voto em vez de se cancelarem.
        """
        from app.visao.validador import alternativas_de_linha, parecidas, validar

        # Aceita entrada de 3 OU 4 elementos. O 4o (confianca por posicao) foi acrescentado
        # depois, e os dubles de teste chamam `_fundir` com 3-tuplas - quebra-los aqui seria
        # o modo de falha que `None vs bool em contrato novo` ja custou a este projeto.
        brutas = [(b[0], b[1], b[2], b[3] if len(b) > 3 else None) for b in brutas]

        detalhes = []
        partes_log = []
        for nome, texto, conf, _por_char in brutas:
            v = validar(texto, formato_hint)
            detalhes.append({"engine": nome,
                             "placa": v[0] if v else None,
                             "padrao": v[1] if v else None,
                             "confianca": round(conf, 3)})
            partes_log.append(_fmt(nome, texto, v))

        # `bruto->validado` e nao so o bruto: a diferenca entre os dois E a informacao. E
        # ela que explica por que duas leituras quase iguais votaram separado - ver `_fmt`.
        lidas = "  ".join(partes_log) or "-"

        # Monta o pool. Cada entrada e (texto, PESO DE VOTO, CONFIANCA ORIGINAL do engine),
        # e os dois numeros existem separados de proposito:
        #
        #  - peso de voto decide a placa. Leitura com mais de 7 caracteres rende varios
        #    candidatos (`OSL12659` -> `OSL1265` e `OSL2659`), e o peso e dividido entre
        #    eles para uma leitura ambigua nao valer mais que uma limpa so por gerar mais
        #    candidatos.
        #  - confianca original e o que vai para o log, para o roteador e para escalar a
        #    confianca final. Usar o peso dividido aqui era um bug meu: uma leitura correta
        #    apoiada por dois engines a 0,85 e 0,90 saia reportando 0,49, porque o `0,90/7`
        #    de um cabecalho lido junto entrava na media. O numero que o atendente ve tem de
        #    descrever o quanto os engines confiaram, nao quantos recortes a string gerou.
        #
        # Uma leitura de 7 chars entra CRUA - fundir cru e validar depois evita que duas
        # correcoes posicionais erradas se somem no voto. Texto mais longo passa por
        # `validar`, que ja tem a ordem de prioridade certa (casamento direto antes de
        # correcao), e ganha as alternativas ESTRUTURAIS de placa de duas linhas.
        pool3: list[tuple[str, object, float]] = []
        for _, texto, conf, por_char in brutas:
            if len(texto) == 7:
                # Peso de voto POR POSICAO quando o engine expoe: e o que impede dois modelos
                # da mesma familia de amplificarem o mesmo erro de caractere.
                pool3.append((texto, por_char or conf, conf))
                continue
            cands: list[str] = []
            principal = validar(texto, formato_hint)
            if principal:
                cands.append(principal[0])
            for alt in alternativas_de_linha(texto):
                if alt not in cands and validar(alt, formato_hint):
                    cands.append(alt)
            for cand in cands:
                # Candidato de texto longo NAO leva o vetor: o `por_char` esta alinhado ao
                # texto ORIGINAL, e o candidato e um recorte dele em outra posicao. Usar o
                # vetor aqui colocaria a confianca de um caractere sobre outro.
                pool3.append((cand, conf / len(cands), conf))

        cru7 = [(t, w) for t, w, _ in pool3]
        conf_original = {}
        for t, _, c in pool3:
            conf_original[t] = max(conf_original.get(t, 0.0), c)
        if not cru7:
            log.info("OCR %s | %s -> NADA (nenhuma leitura com 7 caracteres)",
                     cabecalho, lidas)
            return {"placa": None, "padrao": None, "confianca": 0.0,
                    "votos": 0, "total_engines": len(brutas), "detalhes": detalhes}

        # Vota so entre leituras do MESMO veiculo, e nunca emite string sem respaldo em
        # leitura real - as duas trancas de `visao.consenso`.
        grupos = agrupar_por_veiculo(cru7)
        pool = grupos[0] if grupos else cru7

        fundida = consenso_caractere(pool, formato=prior_de_formato(pool))
        v = validar(fundida, formato_hint) if fundida else None

        if v and leitura_real_proxima(v[0], pool):
            placa, padrao = v
            # Confianca = media das CONFIANCAS ORIGINAIS das leituras que sustentam a placa
            # fundida. Media e nao maximo porque a fusao e resultado do conjunto, e reportar
            # o melhor membro venderia como certeza o que e consenso; original e nao peso de
            # voto porque o peso pode ter sido dividido entre candidatos da mesma leitura.
            # `peso_medio` no fallback, e nao `w` cru: com peso POR POSICAO o `w` e uma
            # lista, e `sum()/len()` sobre lista estouraria. `conf_original` cobre todo `t`
            # do pool, entao o fallback nao deve disparar - mas depender disso e depender de
            # um invariante a duas inferencias de distancia.
            apoio = [conf_original.get(t, peso_medio(w))
                     for t, w in pool if parecidas(t, placa, 2)]
            conf = sum(apoio) / len(apoio) if apoio else max(
                conf_original.get(t, peso_medio(w)) for t, w in pool)
            # `parecidas(..., 2)` e nao `== placa`: `placa` e o resultado de `validar`, que
            # pode ter aplicado ate MAX_CORRECOES trocas digito<->letra, enquanto `pool` tem
            # os textos CRUS. Tres modelos lendo `FBI0I23`/`FBI0I23`/`FBI0123` fundem e
            # validam para `FBI0123`, e a contagem exata dava votos=0 -> `max(votos,1)` = 1:
            # o payload publico reportava "1 engine concordou" quando tres concordaram, e com
            # `ocr_votos_minimos >= 2` o pipeline DESCARTAVA a leitura correta. A linha logo
            # acima ja usava `parecidas` para o mesmo pool ao calcular a confianca — a
            # contagem e que tinha ficado para tras. (Auditoria 27/08/2026, achado M4.)
            votos = sum(1 for t, _ in pool if parecidas(t, placa, 2))
            log.info("OCR %s | %s -> %s conf=%.2f (fusao de %d leitura(s), %d exata(s))",
                     cabecalho, lidas, placa, conf, len(pool), votos)
            return {"placa": placa, "padrao": padrao, "confianca": round(conf, 3),
                    "votos": max(votos, 1), "total_engines": len(brutas),
                    "detalhes": detalhes}

        # A fusao nao validou (ou nao teve respaldo): cai para a leitura individual mais
        # confiante que validou. Sem este ramo, um recorte em que so um membro acerta e os
        # outros devolvem ruido perderia a unica leitura boa que existia.
        validas = [(d["placa"], d["padrao"], d["confianca"]) for d in detalhes if d["placa"]]
        if validas:
            placa, padrao, conf = max(validas, key=lambda x: x[2])
            log.info("OCR %s | %s -> %s conf=%.2f (fusao sem respaldo, melhor individual)",
                     cabecalho, lidas, placa, conf)
            return {"placa": placa, "padrao": padrao, "confianca": round(conf, 3),
                    "votos": 1, "total_engines": len(brutas), "detalhes": detalhes}

        log.info("OCR %s | %s -> NADA (nenhum engine validou)", cabecalho, lidas)
        return {"placa": None, "padrao": None, "confianca": 0.0,
                "votos": 0, "total_engines": len(brutas), "detalhes": detalhes}

class AutoOCRPaddle(AutoOCR):
    """AutoOCR + PaddleOCR como um voto a mais no MESMO pool.

    O PaddleOCR (PP-OCR, Apache-2.0) le placa antiga/borrada real melhor que os modelos de
    linha unica, e vale o que custa: medido nas 30 fotos rotuladas, o pool sem ele acerta
    17/26 em carro e com ele 18/26. E caro (747 ms por recorte, contra 62 ms dos tres
    modelos do fast juntos), e por isso entra so na leitura reativa do GET, que tolera a
    latencia. O monitoramento continuo segue com `AutoOCR`.

    O QUE SAIU DAQUI, em 25/08/2026: a prioridade do Paddle em moto e o gate de nitidez.

    A prioridade ("em moto o Paddle SOBREPOE o AutoOCR") vinha de uma medicao de
    "PaddleOCR 22/27 contra 2/27, nas 27 motos de testes/dataset.json". Aquele dataset tinha
    42 fotos e foi cortado para 29 pelo commit d49a78f, "Remove as placas sinteticas do
    dataset de testes": as 27 motos eram SINTETICAS, e foram deletadas justamente por
    inverter o sinal da medicao. Hoje o dataset tem 2 motos reais, e no recorte real da
    OSL2659 o Paddle devolve string vazia. A regra estava calibrada no dado que a diretriz
    do projeto considera nao confiavel.

    O gate de nitidez ("crop nitido: nao roda o Paddle se o AutoOCR ja validou") era uma
    economia de latencia que decidia por adivinhacao qual engine bastava. E o mesmo tipo de
    escolha-antecipada que fazia a leitura boa morrer. Agora todos leem e a fusao decide;
    o custo total do GET caiu de ~981 ms (easyocr 212 + fast 22 + paddle 747) para ~809 ms
    (fast 62 + paddle 747), porque a EasyOCR saiu do caminho.
    """

    def __init__(self, tesseract_psm: int = 7,
                 deskew_ativo: bool = True, deskew_angulo_max: float = 30.0,
                 fast_modelos: tuple[str, ...] | None = None,
                 usar_easyocr: bool = False):
        super().__init__(tesseract_psm, deskew_ativo=deskew_ativo,
                         deskew_angulo_max=deskew_angulo_max,
                         fast_modelos=fast_modelos, usar_easyocr=usar_easyocr)
        self._paddle = OCR(engine="paddleocr", tesseract_psm=tesseract_psm,
                           deskew_ativo=deskew_ativo, deskew_angulo_max=deskew_angulo_max)

    def carregar(self) -> None:
        super().carregar()
        # O Paddle e REFORCO, nao requisito: ele acrescenta um voto que ajuda em placa
        # antiga borrada. Se ele nao sobe, a leitura tem de continuar com os outros
        # engines, degradada — nunca falhar.
        #
        # Sem este try, uma falha nativa do paddle derruba a LEITURA INTEIRA com 500.
        # Aconteceu em campo (02/09/2026, maquina da feira): o `libpaddle.pyd` nao
        # carregou porque o Visual C++ Redistributable tinha acabado de ser instalado e
        # ainda exigia reboot (instalador devolveu 3010). O fast-plate-ocr estava lendo a
        # placa com conf 0,95 no mesmo instante, e mesmo assim o botao "Ler Placa"
        # respondia 500 — o sistema jogava fora uma leitura boa por causa de um engine
        # acessorio.
        #
        # `_paddle = None` (e nao so logar) porque `_engines` monta a lista de votantes a
        # cada leitura: deixar o objeto meio-carregado ali faria a falha se repetir em
        # todo recorte, com o custo de uma tentativa de carga nativa por vez.
        try:
            self._paddle.carregar()
        except Exception as e:
            log.warning(
                "PaddleOCR indisponivel (%s) — seguindo SEM ele. A leitura continua com "
                "os demais engines; espere queda so em placa antiga borrada. Causa comum "
                "no Windows: Visual C++ Redistributable recem-instalado exigindo reboot.",
                e)
            self._paddle = None

    def _engines(self):
        """Os membros do AutoOCR mais o Paddle. Toda a fusao e herdada.

        Este metodo E a integracao inteira do Paddle: nao ha mais `ler_detalhado` proprio,
        nem arbitragem, nem thread. O que antes eram ~120 linhas de "quem ganha de quem"
        virou "o Paddle e mais um voto", porque e isso que ele e.

        `self._paddle` vem None quando a carga dele falhou (ver `carregar`) — nesse caso a
        lista sai sem ele e a fusao acontece normalmente entre os que sobraram.
        """
        base = super()._engines()
        return base + [("paddleocr", self._paddle)] if self._paddle is not None else base


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

        # Mesmo `formato_hint` que o AutoOCR passa a `validar()`, e pela mesma razão de
        # sempre: os dois caminhos de OCR têm de tratar o header igual.
        #
        # Só a forma fraca ('mercosul', pela cor). O hint forte 'mercosul_moto' saiu em
        # 25/08/2026 dos DOIS caminhos — ele reescrevia caractere sem ver a confiança por
        # caractere e o detector de faixa que o alimentava errou nos dois sentidos nas duas
        # motos medidas (ver `AutoOCR.ler_detalhado` e o docstring de `validador._validar_7`).
        # `e_moto` deixou de ser calculado aqui porque não havia mais o que decidir com ele:
        # ele não alimenta `deteccoes.tipo_veiculo` (essa coluna vem da classe do detector de
        # veículo desde 20/08/2026) e não escolhe mais engine nem hint.
        formato_hint = ""
        if crop is not None and crop.ndim == 3 and crop.size > 0 and self._ocrs:
            _, tinha_header, e_mercosul_header = self._ocrs[0]._remover_header(crop)
            if tinha_header and e_mercosul_header:
                formato_hint = "mercosul"

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


def modelos_fast_da_config(cfg: dict) -> tuple[str, ...]:
    """Membros do ensemble do fast-plate-ocr declarados na config, ou o default medido.

    Config vazia devolve `FAST_MODELOS_DEFAULT` e nao uma tupla vazia: vazio aqui
    significaria "nenhum modelo de OCR", que nao e um estado que alguem queira pedir por
    omissao.
    """
    bruto = str(cfg.get("ocr_fast_modelos", "") or "")
    nomes = tuple(m.strip() for m in bruto.split(",") if m.strip())
    return nomes or FAST_MODELOS_DEFAULT


def obter_ocr_leitura(cfg: dict):
    """OCR de alta acurácia para a leitura GET. Com ocr_engine=auto e ocr_leitura_paddle=sim,
    usa o ensemble AutoOCRPaddle (reforço PaddleOCR para placa borrada). Carregado uma vez.
    O stream ao vivo continua com o OCR mais leve do pipeline."""
    engine = cfg.get("ocr_engine", "auto")
    psm = int(cfg.get("tesseract_psm", "7"))
    usar_paddle = str(cfg.get("ocr_leitura_paddle", "sim")).strip().lower() in ("sim", "true", "1")
    usar_easy = str(cfg.get("ocr_leitura_easyocr", "nao")).strip().lower() in ("sim", "true", "1")
    extras = [e.strip() for e in cfg.get("ocr_engines_extra", "").split(",") if e.strip()]
    deskew_on = str(cfg.get("deskew_ativo", "sim")).strip().lower() in ("sim", "true", "1", "yes")
    deskew_max = float(cfg.get("deskew_angulo_max", "30"))
    fast_modelos = modelos_fast_da_config(cfg)
    # `usar_easy` e `fast_modelos` PRECISAM entrar na chave: ela e o que decide se a
    # instancia cacheada continua servindo. Config nova fora da chave e ajuste que o
    # operador salva, ve confirmado na tela e que nunca chega ao "Ler Placa" - foi o que
    # aconteceu com o detector de leitura e esta registrado no historico do projeto.
    ident = (engine, psm, usar_paddle, usar_easy, fast_modelos,
             tuple(extras), deskew_on, deskew_max)

    def _construir():
        if engine == "auto" and usar_paddle:
            novo = AutoOCRPaddle(tesseract_psm=psm,
                                 deskew_ativo=deskew_on, deskew_angulo_max=deskew_max,
                                 fast_modelos=fast_modelos, usar_easyocr=usar_easy)
        elif engine == "auto":
            novo = AutoOCR(tesseract_psm=psm,
                           deskew_ativo=deskew_on, deskew_angulo_max=deskew_max,
                           fast_modelos=fast_modelos, usar_easyocr=usar_easy)
        elif extras:
            novo = MultiOCR(engines=[engine] + extras, tesseract_psm=psm,
                            deskew_ativo=deskew_on, deskew_angulo_max=deskew_max)
        else:
            novo = OCR(engine=engine, tesseract_psm=psm,
                       deskew_ativo=deskew_on, deskew_angulo_max=deskew_max)
        novo.carregar()
        log.info("OCR de leitura (GET) carregado: engine=%s paddle=%s easyocr=%s "
                 "deskew=%s fast=[%s]", engine, usar_paddle, usar_easy, deskew_on,
                 ", ".join(fast_modelos))
        return novo

    def _definir(v, i):
        global _ocr_leitura, _ocr_leitura_id
        _ocr_leitura, _ocr_leitura_id = v, i

    return _fab.resolver(lambda: (_ocr_leitura, _ocr_leitura_id), ident,
                         _ocr_leitura_criacao_lock, _construir, _definir)


# OCR do perfil RÁPIDO — slots próprios pelo mesmo motivo do detector: `_ocr_leitura` é um
# slot único indexado por `ident`, e chamar `obter_ocr_leitura` com `ocr_leitura_paddle`
# desligado despejaria o ensemble com Paddle e o recarregaria na chamada completa
# seguinte. Lock de USO próprio para que uma leitura rápida não fique atrás de uma
# completa na fila.
_ocr_rapido = None
_ocr_rapido_id: tuple | None = None
ocr_rapido_lock = threading.Lock()
_ocr_rapido_criacao_lock = threading.Lock()


def obter_ocr_rapido(cfg: dict):
    """OCR do perfil rápido: o MESMO ensemble que o stream ao vivo usa, cacheado à parte.

    A diferença que importa é a ausência do PaddleOCR: ~747 ms por recorte contra ~62 ms
    do ensemble do fast-plate-ocr inteiro (números medidos e registrados no comentário de
    `Pipeline.__init__`). Num orçamento de 5 segundos, o Paddle sozinho comeria boa parte
    de cada passada.

    O preço é conhecido: o Paddle entrou no projeto justamente para dar conta de placa
    antiga borrada. Essa é a leitura que o modo rápido vai perder.

    `usar_paddle` não é parâmetro nem lido da config aqui de propósito — ele é o que
    distingue este perfil do completo, e deixá-lo configurável apagaria a distinção.
    """
    engine = cfg.get("ocr_engine", "auto")
    psm = int(cfg.get("tesseract_psm", "7"))
    usar_easy = str(cfg.get("ocr_leitura_easyocr", "nao")).strip().lower() in ("sim", "true", "1")
    extras = [e.strip() for e in cfg.get("ocr_engines_extra", "").split(",") if e.strip()]
    deskew_on = str(cfg.get("deskew_ativo", "sim")).strip().lower() in ("sim", "true", "1", "yes")
    deskew_max = float(cfg.get("deskew_angulo_max", "30"))
    fast_modelos = modelos_fast_da_config(cfg)
    # Mesmos campos de `obter_ocr_leitura` menos `usar_paddle`, que aqui é constante.
    ident = (engine, psm, usar_easy, fast_modelos, tuple(extras), deskew_on, deskew_max)

    def _construir():
        if engine == "auto":
            novo = AutoOCR(tesseract_psm=psm,
                           deskew_ativo=deskew_on, deskew_angulo_max=deskew_max,
                           fast_modelos=fast_modelos, usar_easyocr=usar_easy)
        elif extras:
            novo = MultiOCR(engines=[engine] + extras, tesseract_psm=psm,
                            deskew_ativo=deskew_on, deskew_angulo_max=deskew_max)
        else:
            novo = OCR(engine=engine, tesseract_psm=psm,
                       deskew_ativo=deskew_on, deskew_angulo_max=deskew_max)
        novo.carregar()
        log.info("OCR rápido carregado: engine=%s (sem paddle) easyocr=%s "
                 "deskew=%s fast=[%s]", engine, usar_easy, deskew_on,
                 ", ".join(fast_modelos))
        return novo

    def _definir(v, i):
        global _ocr_rapido, _ocr_rapido_id
        _ocr_rapido, _ocr_rapido_id = v, i

    return _fab.resolver(lambda: (_ocr_rapido, _ocr_rapido_id), ident,
                         _ocr_rapido_criacao_lock, _construir, _definir)
