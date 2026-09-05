"""API de testes — gerencia dataset rotulado e executa avaliações de precisão."""
from __future__ import annotations
import json
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File

router = APIRouter(prefix="/api/testes")

_DATASET = Path("testes/dataset.json")
_RESULTADOS = Path("testes/resultados")
_SNAPSHOTS = Path("app/web/static/snapshots")
_FOTOS_TESTE = Path("testes/fotos")

# Capturas que um humano olhou e recusou (ilegível, duplicada, sem placa). Sem esta
# lista elas voltariam para a fila de classificação a cada carga da tela, e a fila
# nunca chegaria ao fim — hoje são centenas de snapshots contra poucas dezenas úteis.
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024   # 25 MB
_DESCARTADOS = Path("testes/descartados.json")

# Rotas síncronas (`def`) rodam em threads do threadpool do Starlette — duas
# classificações concorrentes (dois admins, ou um duplo-clique) podem cair em threads
# diferentes ao mesmo tempo. Sem lock, a leitura-modificação-escrita do JSON perde
# silenciosamente a rotulagem de uma das duas: a segunda escrita sobrescreve o arquivo
# a partir de uma leitura feita ANTES da primeira escrita. Um lock por arquivo (e não um
# só) porque dataset e descartados são independentes — travar um não precisa bloquear o
# outro.
#
# ORDEM: quando os dois são necessários ao mesmo tempo (só em `classificar`, que decide
# rotular um irmão a partir da lista de descartados), adquira SEMPRE `_lock_dataset`
# primeiro e `_lock_descartados` depois. É a única aquisição aninhada do módulo; inverter
# a ordem em algum ponto novo criaria ciclo de espera entre as duas rotas.
_lock_dataset = threading.Lock()
_lock_descartados = threading.Lock()


def _ler_descartados() -> set[str]:
    if not _DESCARTADOS.exists():
        return set()
    return set(json.loads(_DESCARTADOS.read_text(encoding="utf-8")).get("arquivos", []))


def _escrever_json_atomico(destino, dados: dict) -> None:
    """Grava JSON sem NUNCA deixar o arquivo num estado meio escrito.

    `write_text` trunca e só então escreve: entre as duas coisas o arquivo existe e está
    incompleto. Os locks deste módulo não bastam porque quem LÊ está em outro módulo e não
    os conhece — `app/core/rotulos.protegidos()` é chamado pelo worker de retenção a cada
    5 minutos e por TODO gatilho de captura de dataset. Pegando o arquivo truncado, o
    `json.loads` levanta, `protegidos()` devolve None e a coleta PARA com "dataset
    ilegível" — enquanto o arquivo está íntegro quando alguém vai olhar, o que torna o
    diagnóstico péssimo. (Auditoria 27/08/2026, achado M11.)

    `os.replace` é atômico no mesmo volume, em POSIX e no Windows: o leitor vê o conteúdo
    antigo ou o novo, nunca metade. O temporário fica ao lado do destino de propósito —
    `tempfile.gettempdir()` pode estar em outro volume, e aí `replace` deixa de ser atômico.
    """
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_suffix(destino.suffix + ".tmp")
    tmp.write_text(json.dumps(dados, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, destino)


def _salvar_descartados(arquivos: set[str]) -> None:
    _escrever_json_atomico(_DESCARTADOS, {"arquivos": sorted(arquivos)})


def _ler_dataset() -> dict:
    if not _DATASET.exists():
        return {"version": 1, "fotos": []}
    return json.loads(_DATASET.read_text(encoding="utf-8"))


def _salvar_dataset(ds: dict) -> None:
    # Atômico: `rotulos.protegidos()` lê este arquivo de outras threads, sem lock nenhum,
    # e é ele que decide o que a limpeza automática pode apagar. Ver `_escrever_json_atomico`.
    _escrever_json_atomico(_DATASET, ds)


def _placa_do_nome(nome: str) -> str:
    """Extrai placa do padrão YYYYMMDDThhmmss_PLACA[_frame].jpg

    O sufixo `_frame` precisa entrar aqui porque o pipeline grava DOIS arquivos por
    detecção (`app/visao/pipeline.py`): o recorte `_PLACA.jpg` e o quadro inteiro
    marcado `_PLACA_frame.jpg`. Exigindo ponto logo após a placa, todo quadro-cena
    chegava na fila de classificação como "o OCR não deixou placa" — com a leitura
    desenhada na própria imagem. Quem classificava redigitava à mão o que o OCR já
    tinha lido, e a conferência contra o OCR se perdia (o `obs` do dataset marca
    'confere com OCR' vs 'corrigido' comparando com esta sugestão).

    Continua NÃO casando o que `app/visao/captura_dataset.py` grava (`_camN-marca.jpg`):
    ali o hífen é proposital, porque aquela captura é justamente o que a leitura errou.
    """
    m = re.match(r"\d{8}T\d{6}_([A-Z0-9]{7})(?:_FRAME)?\.", nome.upper())
    return m.group(1) if m else ""


@router.get("/snapshots")
def listar_snapshots():
    arquivos = []

    # `url` é como o NAVEGADOR busca a imagem; `arquivo` é o caminho no DISCO, relativo à
    # raiz do repositório, porque é o que vai para o dataset e o harness abre com
    # `_ROOT / arquivo`. Para os snapshots os dois diferem: a pasta é servida em
    # /static/snapshots mas mora em app/web/static/snapshots. Enquanto `arquivo` levava o
    # prefixo de URL, toda foto de snapshot adicionada ao dataset virava "arquivo não
    # encontrado" na medição — e isso entrava na conta como erro de OCR.
    def _add_dir(pasta: Path, url_prefix: str, arquivo_prefix: str):
        if not pasta.exists():
            return
        for f in sorted(pasta.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                continue
            # `tipo` decide se a foto vai direto pro OCR (crop) ou passa antes pelo
            # detector (frame). Errar aqui mede a coisa errada: um quadro inteiro
            # entregue como recorte manda o OCR ler a cena toda.
            nome_min = f.name.lower()
            if (f.name.startswith("preview_") or nome_min.endswith("_frame.jpg")
                    or "-amostra." in nome_min):
                tipo = "frame"
            else:
                tipo = "crop"
            arquivos.append({
                "nome": f.name,
                "arquivo": f"{arquivo_prefix}/{f.name}",
                "url": f"/{url_prefix}/{f.name}",
                "tipo": tipo,
                "placa_detectada": _placa_do_nome(f.name),
                "tamanho": f.stat().st_size,
                "data": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                "origem": "alpr" if pasta == _SNAPSHOTS else "upload",
            })

    _add_dir(_SNAPSHOTS, "static/snapshots", str(_SNAPSHOTS).replace("\\", "/"))
    _add_dir(_FOTOS_TESTE, "testes/fotos", str(_FOTOS_TESTE).replace("\\", "/"))
    arquivos.sort(key=lambda x: x["data"], reverse=True)
    return arquivos


@router.post("/upload")
async def upload_foto(file: UploadFile = File(...)):
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "Apenas imagens são aceitas")
    ext = Path(file.filename or "foto.jpg").suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png"):
        raise HTTPException(400, "Formato não suportado (use JPG ou PNG)")
    _FOTOS_TESTE.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    nome = f"{ts}_{uuid.uuid4().hex[:6]}{ext}"
    dest = _FOTOS_TESTE / nome
    # Teto de tamanho: `read()` sem limite carrega o corpo inteiro em RAM. Rota é
    # admin-only, mas "admin" não é motivo para aceitar um upload de 4 GB.
    # (Auditoria 27/08/2026.)
    conteudo = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(conteudo) > _MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Imagem maior que %d MB." % (_MAX_UPLOAD_BYTES // (1024 * 1024)))
    dest.write_bytes(conteudo)
    return {
        "ok": True,
        "nome": nome,
        "arquivo": f"testes/fotos/{nome}",
        "url": f"/testes/fotos/{nome}",
        "tamanho": len(conteudo),
    }


@router.get("/candidatos")
def listar_candidatos():
    """Fila de classificação: o que o ALPR capturou e ninguém rotulou nem recusou ainda.

    A placa que vem no nome do arquivo é O QUE O OCR LEU, não a verdade. Ela é devolvida
    como `placa_sugerida` (e nunca como `placa_correta`) porque aceitá-la em massa faria
    o dataset medir o OCR contra ele mesmo: a acurácia iria a ~100% sem significar nada.
    Quem classifica precisa conferir contra a imagem.
    """
    with _lock_dataset:
        no_dataset = {f["arquivo"] for f in _ler_dataset()["fotos"]}
    with _lock_descartados:
        descartados = _ler_descartados()
    fila = [
        s for s in listar_snapshots()
        if s["arquivo"] not in no_dataset and s["arquivo"] not in descartados
        # `preview_bico_N.jpg` é sobrescrito a cada leitura daquele bico. Rotular um
        # deles cria entrada de dataset cujo CONTEÚDO muda sozinho — foi assim que
        # `preview_4.jpg` virou uma linha apontando para arquivo inexistente.
        and not Path(s["arquivo"]).name.startswith("preview_")
    ]
    for s in fila:
        s["placa_sugerida"] = s.pop("placa_detectada", "")
    return {
        "candidatos": fila,
        "total": len(fila),
        "no_dataset": len(no_dataset),
        "descartados": len(descartados),
    }


@router.post("/descartar")
def descartar_candidato(payload: dict):
    """Tira uma captura da fila sem colocá-la no dataset."""
    arquivo = (payload.get("arquivo") or "").strip()
    if not arquivo:
        raise HTTPException(400, "arquivo obrigatório")
    with _lock_descartados:
        d = _ler_descartados()
        d.add(arquivo)
        _salvar_descartados(d)
    return {"ok": True, "descartados": len(d)}


# Teto de arquivos por chamada de lote. Nao e limite de negocio, e freio: a rota grava o
# `descartados.json` inteiro a cada chamada, e um payload de 50 mil nomes viraria um arquivo
# de megabytes escrito sob lock enquanto a tela espera.
MAX_DESCARTE_LOTE = 5000


@router.post("/descartar-lote")
def descartar_lote(payload: dict):
    """Descarta VARIOS candidatos numa chamada.

    A rota de um-por-vez existe e continua sendo a que a tela usa no clique individual. Esta
    existe porque a fila tem 5.730 itens e ~96% deles ninguem vai rotular (3.139 sao
    `-amostra`, que e quadro inteiro e exige cacar a placa dentro dele): limpar isso pela
    rota unitaria seriam milhares de requisicoes HTTP.

    Nao apaga arquivo — so marca em `descartados.json`, o mesmo formato e o mesmo lock da
    rota unitaria, para o "desfazer descarte" que a tela ja tem continuar funcionando item
    por item.
    """
    arquivos = payload.get("arquivos")
    if not isinstance(arquivos, list) or not arquivos:
        raise HTTPException(400, "arquivos: lista obrigatoria e nao vazia")
    if len(arquivos) > MAX_DESCARTE_LOTE:
        raise HTTPException(400, f"lote de {len(arquivos)} acima do teto de "
                                 f"{MAX_DESCARTE_LOTE} — divida em partes")
    nomes = [a.strip() for a in arquivos if isinstance(a, str) and a.strip()]
    if not nomes:
        raise HTTPException(400, "nenhum arquivo valido no lote")
    with _lock_descartados:
        d = _ler_descartados()
        antes = len(d)
        d.update(nomes)
        _salvar_descartados(d)
    # `novos` e nao `len(nomes)`: reenviar um lote ja descartado tem de responder 0, senao
    # quem chama nao distingue "descartei 3.139" de "cliquei duas vezes".
    return {"ok": True, "novos": len(d) - antes, "descartados": len(d)}


@router.delete("/descartar")
def restaurar_candidato(payload: dict):
    """Desfaz um descarte — devolve a captura para a fila."""
    arquivo = (payload.get("arquivo") or "").strip()
    with _lock_descartados:
        d = _ler_descartados()
        if arquivo not in d:
            raise HTTPException(404, "Arquivo não está na lista de descartados")
        d.discard(arquivo)
        _salvar_descartados(d)
    return {"ok": True, "descartados": len(d)}


@router.get("/dataset")
def obter_dataset():
    with _lock_dataset:
        return _ler_dataset()


def _upsert_foto(ds: dict, arquivo: str, placa: str, formato: str, tipo: str,
                 obs: str, extras: dict) -> None:
    """Insere ou atualiza uma foto do dataset, casando por `arquivo`."""
    existente = next((f for f in ds["fotos"] if f["arquivo"] == arquivo), None)
    if existente:
        existente.update({
            "placa_correta": placa,
            "formato": formato or _inferir_formato(placa),
            "tipo": tipo,
            "obs": obs or existente.get("obs", ""),
            **extras,
        })
    else:
        ds["fotos"].append({
            "id": uuid.uuid4().hex[:8],
            "arquivo": arquivo,
            "placa_correta": placa,
            "formato": formato or _inferir_formato(placa),
            "tipo": tipo,
            "obs": obs,
            **extras,
        })


# Nome que o pipeline gera para uma detecção: `TS_PLACA.jpg` (recorte) e
# `TS_PLACA_frame.jpg` (quadro inteiro marcado). Só esse par é irmão.
_PAR_PIPELINE = re.compile(r"^(\d{8}T\d{6}_[A-Z0-9]{7})(_FRAME)?\.(JPG|JPEG|PNG)$")


def _irmao_do_par(arquivo: str) -> tuple[str, str] | None:
    """Devolve (caminho, tipo) do arquivo irmão de uma detecção, ou None.

    O pipeline grava DOIS arquivos por detecção (`app/visao/pipeline.py`): o recorte da
    placa e o quadro inteiro com a caixa desenhada. É o MESMO veículo e a MESMA placa —
    o recorte sai de dentro do bbox do quadro —, então rotular um determina o outro e
    fazer o humano digitar a mesma placa duas vezes é só trabalho repetido.

    O que NÃO se pode fazer é colapsar os dois em um: eles medem etapas diferentes. O
    recorte (`tipo: crop`) vai direto ao OCR; o quadro (`tipo: frame`) passa antes pelo
    detector. Descartar um lado apagaria justamente a separação entre erro de detecção
    e erro de OCR. Por isso a placa se propaga e os dois ficam no dataset.
    """
    p = Path(arquivo)
    if p.parent != _SNAPSHOTS:          # uploads e capturas de bico não têm par
        return None
    m = _PAR_PIPELINE.match(p.name.upper())
    if not m:
        return None
    base, era_frame = m.group(1), bool(m.group(2))
    # O irmão preserva a extensão real do arquivo original (o regex casou em maiúsculas).
    nome_irmao = p.name[:len(base)] + ("" if era_frame else "_frame") + p.suffix
    irmao = p.parent / nome_irmao
    if not irmao.exists():
        return None
    return irmao.as_posix(), ("crop" if era_frame else "frame")


@router.post("/dataset")
def adicionar_foto(payload: dict):
    arquivo = (payload.get("arquivo") or "").strip()
    placa = (payload.get("placa_correta") or "").upper().strip()
    if not arquivo or not placa:
        raise HTTPException(400, "arquivo e placa_correta são obrigatórios")

    # Todo o ciclo leitura-modificação-escrita precisa ficar atrás do lock, senão duas
    # classificações concorrentes leem o mesmo estado antigo e a segunda escrita apaga a
    # primeira em silêncio (ver comentário perto de `_lock_dataset`).
    with _lock_dataset:
        ds = _ler_dataset()
        # Atualiza se já existe, senão insere
        # Origem (bico/posto) fica gravada para dar para saber de onde veio cada foto e,
        # depois, medir acurácia por posto — o dataset agora mistura vários clientes.
        origem = {k: payload[k] for k in ("bico_id", "origem") if payload.get(k)}

        # `layout` é campo próprio, e não texto em `obs`. Moto e carro são problemas
        # diferentes de OCR (a placa de moto é empilhada em duas linhas e chega com bem
        # menos pixels), mas `formato` só distingue mercosul/antigo — então o relatório
        # não conseguia mostrar a taxa de moto, que é justamente a que está em questão.
        layout = (payload.get("layout") or "").strip().lower()
        if layout and layout not in ("carro", "moto"):
            raise HTTPException(400, "layout deve ser 'carro' ou 'moto'")
        if layout:
            origem["layout"] = layout

        formato = payload.get("formato") or ""
        _upsert_foto(ds, arquivo, placa, formato, payload.get("tipo", "crop"),
                     payload.get("obs", ""), origem)

        # Propaga para o irmão do par recorte/quadro: mesma detecção, mesma placa.
        #
        # `_lock_descartados` cobre da CHECAGEM até a GRAVAÇÃO (ver ORDEM na definição dos
        # locks). Só ler a lista sob o lock não bastaria: um `/descartar` concorrente ainda
        # caberia entre a checagem e o `_salvar_dataset`, e o irmão terminaria descartado E
        # rotulado — a máquina desfazendo a recusa explícita de quem olhou a imagem, que é
        # justamente o que o `if` abaixo existe para impedir.
        propagado = None
        with _lock_descartados:
            par = _irmao_do_par(arquivo)
            if par:
                irmao, tipo_irmao = par
                ja_rotulado = any(f["arquivo"] == irmao for f in ds["fotos"])
                # Um irmão já rotulado carrega o julgamento explícito de um humano, e um
                # irmão descartado carrega a recusa explícita dele (quadro ilegível,
                # veículo saindo). Sobrescrever qualquer um dos dois seria a máquina
                # desfazendo a decisão de quem olhou a imagem.
                if not ja_rotulado and irmao not in _ler_descartados():
                    # `obs` registra que NINGUÉM olhou este arquivo específico: a placa veio
                    # do irmão. Sem essa marca não daria para separar, numa medição estranha,
                    # o que foi conferido do que foi herdado.
                    _upsert_foto(ds, irmao, placa, formato, tipo_irmao,
                                 f"propagado do par ({Path(arquivo).name})", origem)
                    propagado = irmao

            _salvar_dataset(ds)
    return {"ok": True, "total": len(ds["fotos"]), "propagado": propagado}


@router.delete("/dataset")
def remover_foto(payload: dict):
    arquivo = (payload.get("arquivo") or "").strip()
    if not arquivo:
        raise HTTPException(400, "arquivo obrigatório")
    with _lock_dataset:
        ds = _ler_dataset()
        antes = len(ds["fotos"])
        ds["fotos"] = [f for f in ds["fotos"] if f["arquivo"] != arquivo]
        if len(ds["fotos"]) == antes:
            raise HTTPException(404, "Foto não encontrada no dataset")
        _salvar_dataset(ds)
    return {"ok": True, "total": len(ds["fotos"])}


@router.post("/rodar")
def rodar_testes(payload: dict = {}):
    import sys
    from pathlib import Path as _Path
    _sys_path_backup = sys.path[:]
    sys.path.insert(0, str(_Path("testes").resolve().parent))
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("run_testes", "testes/run_testes.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        engines = payload.get("engines") or None
        salvar = bool(payload.get("salvar", False))
        if not engines:
            from app.core import config as cfg_mod
            cfg = cfg_mod.carregar()
            engines = [cfg.get("ocr_engine", "auto")]
        resultado = mod.rodar(engines=engines, salvar=salvar)
        return resultado
    except Exception as e:
        # A mensagem interna da exceção (texto do SQLite, caminho de arquivo) fica no
        # LOG, não na resposta ao cliente. (Auditoria 27/08/2026.)
        log.error("Falha em a execução dos testes: %s" % e, exc_info=True)
        raise HTTPException(500, "Operação falhou. Veja o log do servidor.")
    finally:
        sys.path[:] = _sys_path_backup


@router.get("/bicos")
def listar_bicos():
    """Bicos cadastrados, com posto e câmera — alvo de captura do dataset.

    Capturar por BICO (e não por câmera) é o que faz o teste medir o mesmo que a
    produção: a leitura reativa analisa o recorte da área do bico, não o frame inteiro.
    """
    from app.core import banco
    empresas = {e["id"]: e for e in banco.empresas_listar()}
    automacoes = {a["id"]: a for a in banco.automacoes_listar()}
    cameras = {c["id"]: c for c in banco.cameras_listar()}
    saida = []
    for b in banco.bicos_listar():
        a = automacoes.get(b["automacao_id"])
        emp = empresas.get(a["empresa_id"]) if a else None
        cam = cameras.get(b["camera_id"])
        # `cameras` descreve os dois slots; os campos avulsos seguem apontando para a
        # primeira, para a tela de testes atual continuar funcionando sem alteração.
        fontes = []
        for _slot, camera_id, roi, papel in banco.slots_do_bico(b):
            c = cameras.get(camera_id)
            fontes.append({"camera_id": camera_id, "papel": papel,
                           "camera_nome": c["nome"] if c else "?",
                           "camera_local": c.get("local", "") if c else "",
                           "tem_roi": bool(roi)})
        saida.append({
            "id": b["id"],
            "codigo": b["codigo"],
            "nome": b["nome"],
            "tem_roi": bool(b["roi"]),
            "cameras": fontes,
            "camera_id": b["camera_id"],
            "camera_nome": cam["nome"] if cam else "?",
            "camera_local": cam.get("local", "") if cam else "",
            "automacao_codigo": a["codigo"] if a else "?",
            "posto": emp["nome"] if emp else "(sem posto)",
            "posto_id": emp["id"] if emp else None,
        })
    return saida


@router.post("/capturar-bico/{bico_id}")
def capturar_bico(bico_id: int, payload: dict = {}):
    """Captura da câmera do bico e salva JÁ RECORTADO pela área dele.

    É exatamente o que o detector recebe em produção — o dataset passa a medir o
    caminho real em vez do frame inteiro.
    """
    import cv2
    import json as _json
    from app.core import banco
    from app.core import config as cfg_mod
    from app.visao import camera as camera_mod
    from app.visao import leitura as leitura_mod

    bico = banco.bicos_obter(bico_id)
    if not bico:
        raise HTTPException(404, "Bico não encontrado")
    # Bico de 2 câmeras: `camera_id` no payload escolhe qual capturar (ausente = a
    # primeira). Cada câmera tem a sua própria área, e capturar com o recorte da outra
    # produziria uma imagem de dataset que não corresponde a nada que a leitura vê.
    escolhida = payload.get("camera_id") or bico["camera_id"]
    if escolhida not in (bico["camera_id"], bico.get("camera2_id")):
        raise HTTPException(400, "Câmera não é deste bico")
    roi_bruto = bico["roi"] if escolhida == bico["camera_id"] else bico.get("roi2")
    cam = banco.cameras_obter(escolhida)
    if not cam:
        raise HTTPException(404, "Câmera do bico não encontrada")

    auto = banco.automacoes_obter(bico["automacao_id"])
    emp = banco.empresas_obter(auto["empresa_id"]) if auto else None
    origem = f"{emp['nome'] if emp else '?'} / bico {bico['codigo']}"

    cfg = cfg_mod.carregar()
    with leitura_mod.lock_camera(cam["id"]):   # 1 conexão RTSP por câmera
        frame = camera_mod.capturar_frame_unico(
            tipo=cam["camera_tipo"],
            indice=cam.get("rtsp_url_custom") or cam.get("camera_indice", "0"),
            largura=int(cfg.get("camera_largura", "1280")),
            altura=int(cfg.get("camera_altura", "720")),
            fps=int(cfg.get("camera_fps", "15")),
            intelbras={
                "host": "" if cam.get("rtsp_url_custom") else cam.get("intelbras_host", ""),
                "porta": cam.get("intelbras_porta", "554"),
                "usuario": cam.get("intelbras_usuario", "admin"),
                "senha": cam.get("intelbras_senha") or cfg.get("intelbras_senha", ""),
                "canal": cam.get("intelbras_canal", "1"),
                "subtype": cam.get("intelbras_subtype", "1"),
                "formato": cam.get("intelbras_formato", "padrao"),
                "rtsp_transporte": cfg.get("rtsp_transporte", "tcp"),
            },
        )
    if frame is None:
        raise HTTPException(503, "Não foi possível capturar imagem da câmera")

    recortado = False
    if roi_bruto and payload.get("aplicar_roi", True):
        r = _json.loads(roi_bruto) if isinstance(roi_bruto, str) else roi_bruto
        recorte = frame[r["y"]:r["y"] + r["h"], r["x"]:r["x"] + r["w"]]
        if recorte.size:
            frame, recortado = recorte, True

    _FOTOS_TESTE.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    # Nome com o CÓDIGO do bico (o que aparece na tela), não o id do banco — quem rotula
    # o dataset lê o nome do arquivo e id != código confunde.
    cod = re.sub(r"[^A-Za-z0-9_-]", "", bico["codigo"])[:12] or str(bico_id)
    nome = f"bico-{cod}_{ts}_{uuid.uuid4().hex[:4]}.jpg"
    dest = _FOTOS_TESTE / nome
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise HTTPException(500, "Falha ao codificar imagem")
    dest.write_bytes(buf.tobytes())

    return {
        "ok": True,
        "nome": nome,
        "arquivo": f"testes/fotos/{nome}",
        "url": f"/testes/fotos/{nome}",
        "tipo": "frame",           # é o que o detector recebe: área do bico, sem recorte de placa
        "recortado_por_roi": recortado,
        "sem_roi": not recortado,
        "largura": int(frame.shape[1]),
        "altura": int(frame.shape[0]),
        "bico_id": bico_id,
        "origem": origem,
        "tamanho": dest.stat().st_size,
    }


@router.get("/resultados")
def listar_resultados():
    _RESULTADOS.mkdir(parents=True, exist_ok=True)
    arquivos = sorted(_RESULTADOS.glob("*.json"), reverse=True)
    return [
        {"nome": f.name, "data": f.name[:15], "tamanho": f.stat().st_size}
        for f in arquivos
    ]


@router.get("/resultados/{nome}")
def obter_resultado(nome: str):
    if "/" in nome or "\\" in nome:
        raise HTTPException(400, "nome inválido")
    path = _RESULTADOS / nome
    if not path.exists():
        raise HTTPException(404, "Resultado não encontrado")
    return json.loads(path.read_text(encoding="utf-8"))


def _inferir_formato(placa: str) -> str:
    import re
    if re.match(r"^[A-Z]{3}[0-9][A-Z][0-9]{2}$", placa):
        return "mercosul"
    if re.match(r"^[A-Z]{3}[0-9]{4}$", placa):
        return "antigo"
    return "desconhecido"
