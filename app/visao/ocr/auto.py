"""Seleção automática de engine e ensembles (AutoOCR, AutoOCRPaddle, MultiOCR)."""
from __future__ import annotations
import logging
import threading

import cv2
import numpy as np

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

    def carregar(self) -> None:
        self._fast.carregar()
        self._easy.carregar()

    def ler(self, crop) -> tuple[str, float]:
        det = self.ler_detalhado(crop)
        self._ultimo_detalhe = det
        return det["placa"] or "", det["confianca"]

    def ler_detalhado(self, crop) -> dict:
        from app.visao.validador import validar

        # Realce (upscale + sharpen) — recupera placas pequenas/borradas antes do OCR.
        crop = _realcar_para_ocr(crop)

        tinha_header = False
        e_mercosul_header = False
        if crop is not None and crop.ndim == 3 and crop.size > 0:
            _, tinha_header, e_mercosul_header = self._fast._remover_header(crop)

        # Moto: aspect ≤ 2 (200×140 vs 400×130) — 2 linhas de texto, easyocr é superior
        aspect = (crop.shape[1] / max(crop.shape[0], 1)) if crop is not None else 3.0
        e_moto = tinha_header and aspect <= 2.0
        self._ultimo_e_moto = e_moto

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
        if tinha_header and e_mercosul_header:
            formato_hint = "mercosul_moto" if e_moto else "mercosul"
        else:
            formato_hint = ""

        tipo_placa = ("moto-mercosul" if e_moto else ("mercosul-carro" if e_mercosul_header else "antigo"))
        h, w = (crop.shape[:2] if crop is not None else (0, 0))
        log.info(
            "AutoOCR: crop=%dx%d aspect=%.2f tipo=%s header=%s mercosul=%s principal=%s",
            w, h, aspect, tipo_placa, tinha_header, e_mercosul_header, principal.engine,
        )

        texto, conf = principal.ler(crop)
        resultado = validar(texto, formato_hint)
        log.info(
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
            log.info("AutoOCR: aceito %r conf=%.2f (sem fallback)", resultado[0], conf)
            return {
                "placa": resultado[0], "padrao": resultado[1],
                "confianca": round(conf, 3),
                "votos": 1, "total_engines": 1, "detalhes": detalhes,
            }

        motivo_fallback = "moto" if e_moto else ("conf_baixa=%.2f" % conf if resultado else "sem_resultado")
        log.info("AutoOCR: rodando fallback=%s motivo=%s", fallback.engine, motivo_fallback)

        # Executa fallback: sempre para moto, ou quando principal falhou/conf baixa
        texto2, conf2 = fallback.ler(crop)
        resultado2 = validar(texto2, formato_hint)
        log.info(
            "AutoOCR %s: bruto=%r → validado=%r conf=%.2f",
            fallback.engine, texto2, resultado2[0] if resultado2 else None, conf2,
        )
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
            log.info(
                "AutoOCR: ambos validaram — %s(%r %.2f) vs %s(%r %.2f) → vencedor=%s(%r)",
                principal.engine, resultado[0], conf,
                fallback.engine, resultado2[0], conf2,
                vencedor, melhor[0],
            )
            return {
                "placa": melhor[0], "padrao": melhor[1],
                "confianca": round(melhor_conf, 3),
                "votos": 1, "total_engines": 2, "detalhes": detalhes,
            }

        if resultado:
            log.info("AutoOCR: somente principal validou → %r conf=%.2f", resultado[0], conf)
            return {
                "placa": resultado[0], "padrao": resultado[1],
                "confianca": round(conf, 3),
                "votos": 1, "total_engines": 2, "detalhes": detalhes,
            }

        if resultado2:
            log.info("AutoOCR: somente fallback validou → %r conf=%.2f", resultado2[0], conf2)
            return {
                "placa": resultado2[0], "padrao": resultado2[1],
                "confianca": round(conf2, 3),
                "votos": 1, "total_engines": 2, "detalhes": detalhes,
            }

        log.info("AutoOCR: nenhum engine validou (principal=%r fallback=%r)", texto, texto2)
        return {
            "placa": None, "padrao": None, "confianca": 0.0,
            "votos": 0, "total_engines": 2, "detalhes": detalhes,
        }


class AutoOCRPaddle(AutoOCR):
    """AutoOCR + PaddleOCR como reforço para placas de LINHA ÚNICA borradas.

    O PaddleOCR (PP-OCR, Apache-2.0) lê muito melhor placas antigas/borradas reais
    (UFPR-ALPR: ~70% vs ~50% do AutoOCR), mas é fraco no limpo e NÃO faz moto (2 linhas).
    Arbitragem, só para não-moto:
      - PaddleOCR e AutoOCR concordam        → mantém.
      - AutoOCR não validou                  → usa PaddleOCR (se validar).
      - Discordam                            → decide pela NITIDEZ do crop:
            nítido (lapvar ≥ limiar) → AutoOCR; borrado (< limiar) → PaddleOCR.
    Moto (2 linhas) sempre fica com o AutoOCR (o PaddleOCR não lê layout empilhado).

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

        # Nitidez decide a estratégia ANTES de rodar qualquer engine (medida é barata:
        # só um Laplaciano). Cada engine sozinho custa segundos num crop pequeno/borrado
        # (medido: AutoOCR ~3s, PaddleOCR ~3s em CPU) — por isso a estratégia muda:
        cinza = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        nitidez = cv2.Laplacian(cinza, cv2.CV_64F).var()
        crop_nitido = nitidez >= self._limiar_nitidez

        if crop_nitido:
            # Nítido: AutoOCR sozinho já é confiável (ver arbitragem abaixo, que sempre
            # manteria o AutoOCR aqui) — roda só ele. Só aciona o Paddle se o AutoOCR não
            # validar nada (raro num crop nítido) ou for moto (nesse caso, mantém AutoOCR).
            d = super().ler_detalhado(crop)
            if getattr(self, "_ultimo_e_moto", False) or d.get("placa") is not None:
                return d
            texto_p, conf_p = self._paddle.ler(crop)
            vp = validar(texto_p)
            if vp:
                placa_p, padrao_p = vp
                d = dict(d)
                d["placa"], d["padrao"], d["confianca"] = placa_p, padrao_p, round(conf_p, 3)
                d.setdefault("detalhes", []).append(
                    {"engine": "paddleocr", "placa": placa_p, "padrao": padrao_p, "confianca": round(conf_p, 3)}
                )
            return d

        # Borrado: os dois PODEM legitimamente contribuir, então rodam EM PARALELO (thread)
        # em vez de sequencial — sequencial custaria a SOMA dos dois (~6s); paralelo custa
        # o MAIOR dos dois (~3s), já que numpy/onnxruntime liberam o GIL durante a inferência.
        # Roda o Paddle mesmo se acabar sendo moto (resultado descartado depois) — não
        # adiciona latência (é concorrente), só usa 1 núcleo extra do servidor dedicado.
        resultado: dict = {}

        def _rodar_auto() -> None:
            resultado["d"] = AutoOCR.ler_detalhado(self, crop)

        t = threading.Thread(target=_rodar_auto, daemon=True)
        t.start()
        texto_p, conf_p = self._paddle.ler(crop)
        t.join()
        d = resultado["d"]

        if getattr(self, "_ultimo_e_moto", False):
            return d  # moto: paddle não ajuda (rodou em paralelo, mas resultado é descartado)

        vp = validar(texto_p)
        if not vp:
            return d
        placa_p, padrao_p = vp
        placa_a = d.get("placa")

        if placa_a == placa_p:
            return d  # concordam

        # Já sabemos que o crop é borrado (nitidez < limiar) — Paddle tem prioridade
        # quando discordam, ou quando o AutoOCR não validou nada.
        log.info("AutoOCRPaddle: discordam auto=%r paddle=%r nitidez=%.0f → paddle",
                 placa_a, placa_p, nitidez)
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
