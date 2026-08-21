"""Replay do detector de veículo sobre os quadros já gravados, para medir `tipo_veiculo`
e o sinal cru por trás dele (`veiculo_classe`/`veiculo_conf`/`tipo_veiculo_fonte`).

Para cada linha de `deteccoes` que tem `frame` e `bbox`, roda o `VehicleDetector` no JPEG
salvo e diz qual `OrigemTipo` a fonte NOVA (classe do YOLOX) daria. Serve a dois usos:

1. **Medir antes/depois.** Rodando com `--comparar`, mostra em quantas linhas a fonte nova
   discorda do que está gravado. Foi assim que se decidiu aposentar o heurístico de
   aspecto: 12 dos 25 rótulos gravados eram 'moto' num posto de combustível, e 11 deles
   não se sustentam.

2. **Recontar o passado** (`--aplicar`). Opcional, e por isso o padrão é só relatar. Grava
   os quatro campos em BLOCO (nunca só `tipo_veiculo`) e prefixa `tipo_veiculo_fonte` com
   `replay:` — é o que distingue, depois, uma linha reconstruída de uma medida ao vivo
   (`SELECT tipo_veiculo_fonte LIKE 'replay:%' ...`).

Limite conhecido, e é o motivo de isto NÃO virar rotina agendada: o `frame` salvo tem o
retângulo e o rótulo da detecção desenhados por cima, então não é byte a byte a imagem que
o detector viu ao vivo. Serve para medir tendência e para auditar linha a linha; não serve
como verdade fundamental.

    ./.venv/Scripts/python.exe testes/recalcula_tipo_veiculo.py --comparar
    ./.venv/Scripts/python.exe testes/recalcula_tipo_veiculo.py --aplicar   # faça backup
"""
from __future__ import annotations

import argparse
import dataclasses
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import cv2  # noqa: E402

from app.core import config  # noqa: E402
from app.visao.detector import OrigemTipo, SEM_VEICULO, _criar_detector_veiculo  # noqa: E402

ESTATICO = RAIZ / "app" / "web"


def _caminho(rel: str | None) -> Path | None:
    """`/static/...` gravado no banco → arquivo em disco."""
    if not rel:
        return None
    p = ESTATICO / rel.lstrip("/")
    return p if p.exists() else None


def _origem_do_quadro(detector, img, bbox: dict) -> OrigemTipo:
    """Origem do tipo de veículo (classe COCO + confiança) para a placa desta bbox,
    reconstruída do quadro salvo.

    O MENOR veículo que contém o centro da bbox, e não o primeiro: com uma moto na frente
    de um carro as duas caixas contêm o ponto, e a menor é a que de fato carrega a placa.
    Em produção esta contenção geométrica não existe — a associação é ESTRUTURAL, porque a
    placa foi encontrada dentro do recorte daquele veículo (`DetectorDoisEstagios`). Aqui
    ela é necessária porque estamos reconstruindo a partir de uma bbox já gravada, sem o
    recorte original de cada veículo.
    """
    cx = bbox["x"] + bbox["w"] / 2
    cy = bbox["y"] + bbox["h"] / 2
    candidatos = [
        (vw * vh, vconf, vcls)
        for vx, vy, vw, vh, vconf, vcls in detector.detectar(img)
        if vx <= cx <= vx + vw and vy <= cy <= vy + vh
    ]
    if not candidatos:
        return SEM_VEICULO
    _area, vconf, vcls = min(candidatos)
    return OrigemTipo.de_classe(vcls, vconf)


def _para_replay(origem: OrigemTipo) -> OrigemTipo:
    """Marca a origem como reconstruída — `banco._deteccoes` aceita o prefixo `replay:` e
    nenhuma consulta de auditoria confunde isto com medida ao vivo."""
    return dataclasses.replace(origem, fonte=f"replay:{origem.fonte}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--comparar", action="store_true",
                    help="detalha as linhas em que a fonte nova discorda do gravado")
    ap.add_argument("--aplicar", action="store_true",
                    help="ESCREVE o resultado na coluna tipo_veiculo (faça backup antes)")
    ap.add_argument("--db", default=str(RAIZ / "placas.db"))
    args = ap.parse_args()

    cfg = config.carregar()
    detector = _criar_detector_veiculo(cfg)
    detector.carregar()
    if detector.sess is None:
        print("VehicleDetector não carregou — sem modelo não há o que medir.")
        return 1

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    linhas = con.execute(
        "SELECT id, placa, tipo_veiculo, frame, bbox FROM deteccoes "
        "WHERE frame IS NOT NULL AND bbox IS NOT NULL ORDER BY id"
    ).fetchall()

    import json
    contagem: Counter = Counter()
    divergentes: list[tuple] = []
    novos: list[OrigemTipo | None] = []   # None = quadro/bbox ilegível, linha pulada
    ids: list[int] = []
    t0 = time.time()

    for i, r in enumerate(linhas, 1):
        img_path = _caminho(r["frame"])
        if img_path is None:
            contagem["quadro-ausente"] += 1
            continue
        img = cv2.imread(str(img_path))
        if img is None:
            contagem["quadro-ilegivel"] += 1
            continue
        try:
            bbox = json.loads(r["bbox"])
        except (ValueError, TypeError):
            contagem["bbox-invalida"] += 1
            continue

        origem = _origem_do_quadro(detector, img, bbox)
        contagem[origem.tipo or "NULL"] += 1
        ids.append(r["id"])
        novos.append(origem)
        if r["tipo_veiculo"] is not None and r["tipo_veiculo"] != origem.tipo:
            divergentes.append((r["id"], r["placa"], r["tipo_veiculo"], origem.tipo))

        if i % 50 == 0:
            print(f"  {i}/{len(linhas)}...", flush=True)

    total = sum(contagem[k] for k in ("carro", "moto", "NULL"))
    seg = time.time() - t0
    print(f"\n{len(linhas)} linhas com quadro+bbox · {seg:.1f}s "
          f"({seg / max(len(linhas), 1):.2f}s/quadro)\n")
    for tipo in ("carro", "moto", "NULL"):
        n = contagem[tipo]
        print(f"  {tipo:<6} {n:>4}  ({100 * n / max(total, 1):.1f}%)")
    for k, v in contagem.items():
        if k not in ("carro", "moto", "NULL"):
            print(f"  ({k}: {v})")

    rotuladas = sum(1 for r in linhas if r["tipo_veiculo"] is not None)
    print(f"\nlinhas com tipo gravado: {rotuladas} · divergem da fonte nova: "
          f"{len(divergentes)}")
    if args.comparar and divergentes:
        print("\n  id     placa      gravado -> fonte nova")
        for id_, placa, antigo, novo in divergentes:
            print(f"  {id_:<6} {placa:<10} {antigo:<8} -> {novo}")

    if args.aplicar:
        # Os quatro campos em BLOCO, e não um UPDATE de tipo_veiculo isolado — mesma regra
        # de `atualizar_deteccao`: um veredito sem o sinal cru correspondente é o defeito
        # que esta coluna existe para evitar.
        linhas_upd = [
            (origem.tipo, origem.classe, origem.conf, _para_replay(origem).fonte, id_)
            for id_, origem in zip(ids, novos)
        ]
        with con:
            con.executemany(
                "UPDATE deteccoes SET tipo_veiculo=?, veiculo_classe=?, veiculo_conf=?, "
                "tipo_veiculo_fonte=? WHERE id=?",
                linhas_upd,
            )
        print(f"\nAplicado em {len(linhas_upd)} linhas (tipo_veiculo_fonte prefixado "
              f"'replay:').")
    else:
        print("\n(nada foi escrito — use --aplicar para gravar)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
