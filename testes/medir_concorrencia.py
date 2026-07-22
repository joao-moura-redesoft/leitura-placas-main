"""Mede como a leitura degrada sob concorrência — para rodar no servidor de produção.

Por que existe: na CPU de desenvolvimento, 2 leituras simultâneas cortaram as
tentativas de foto de 12 para 8 cada (33% a menos de evidência por leitura), porque
o detector/OCR têm lock global (1 inferência por vez no processo inteiro) e o loop de
leitura é limitado por TEMPO, não por número de fotos — sob disputa, cada leitura
simplesmente consegue menos fotos dentro do mesmo orçamento de ~28s. A latência quase
não muda (por isso ninguém percebe no relógio); quem cai é a taxa de acerto, e só
aparece quando o movimento aumenta. Não há como saber se isso se repete na GPU de
produção sem medir lá — é a única forma de responder "quantos postos cabem por
servidor" com dado real em vez de suposição.

Uso (do servidor de produção, com a aplicação já rodando):

    python testes/medir_concorrencia.py --bicos 2,3,4,5

Mede 1, 2 e N leituras simultâneas (N = quantidade de bicos passados), disparando
POST /api/bicos/{id}/ler-placa-teste em paralelo (marca como origem "teste" — não
conta como chamada do roteador nas estatísticas de integração). Cada bico precisa já
ter câmera e área de captura cadastradas.

Argumentos:
  --bicos       IDs de bico a usar, separados por vírgula (obrigatório).
                Ideal ter bicos de câmeras DIFERENTES, para medir o gargalo dos
                modelos (lock global) e não o lock por câmera (que já é esperado).
  --niveis      Níveis de concorrência a testar, separados por vírgula
                (padrão: "1,2,<quantidade de bicos>").
  --url         Base do servidor (padrão: http://localhost:14000;
                troque para o endereço real do servidor de produção).
  --api-key     Se o servidor exigir (cabeçalho X-API-Key).
  --repeticoes  Quantas vezes repetir cada nível, para tirar média (padrão: 1).
"""
from __future__ import annotations
import argparse
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests


def ler_bico(base_url: str, bico_id: int, api_key: str | None, timeout: float) -> dict:
    headers = {"X-API-Key": api_key} if api_key else {}
    inicio = time.time()
    try:
        r = requests.post(
            f"{base_url}/api/bicos/{bico_id}/ler-placa-teste",
            headers=headers, timeout=timeout,
        )
        duracao = time.time() - inicio
        if r.status_code != 200:
            return {"bico_id": bico_id, "ok": False, "duracao": duracao,
                     "erro": f"HTTP {r.status_code}: {r.text[:120]}"}
        d = r.json()
        return {"bico_id": bico_id, "ok": True, "duracao": duracao,
                 "tentativas": d.get("tentativas"), "acordo": d.get("acordo"),
                 "parada_motivo": d.get("parada_motivo"), "placa": d.get("placa")}
    except requests.RequestException as e:
        return {"bico_id": bico_id, "ok": False, "duracao": time.time() - inicio, "erro": str(e)}


def rodar_nivel(base_url: str, bico_ids: list[int], api_key: str | None, timeout: float) -> list[dict]:
    """Dispara len(bico_ids) leituras ao mesmo tempo — esse número É o nível de concorrência."""
    with ThreadPoolExecutor(max_workers=len(bico_ids)) as ex:
        futuros = [ex.submit(ler_bico, base_url, bid, api_key, timeout) for bid in bico_ids]
        return [f.result() for f in as_completed(futuros)]


def resumir(resultados: list[dict]) -> str:
    ok = [r for r in resultados if r["ok"]]
    falhas = len(resultados) - len(ok)
    partes = [f"{len(ok)}/{len(resultados)} OK"]
    if falhas:
        partes.append(f"{falhas} falha(s)")
    if ok:
        tentativas = [r["tentativas"] for r in ok if r.get("tentativas") is not None]
        duracoes = [r["duracao"] for r in ok]
        if tentativas:
            partes.append(f"tentativas: média {statistics.mean(tentativas):.1f} "
                           f"(min {min(tentativas)}, max {max(tentativas)})")
        partes.append(f"duração: média {statistics.mean(duracoes):.1f}s "
                       f"(min {min(duracoes):.1f}s, max {max(duracoes):.1f}s)")
    return " | ".join(partes)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bicos", required=True, help="IDs de bico separados por vírgula, ex: 2,3,4,5")
    ap.add_argument("--niveis", default=None, help='Níveis de concorrência, ex: "1,2,4" (padrão: 1, 2 e todos)')
    ap.add_argument("--url", default="http://localhost:14000")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--repeticoes", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=60.0, help="Timeout por chamada, em segundos")
    args = ap.parse_args()

    todos_bicos = [int(b.strip()) for b in args.bicos.split(",") if b.strip()]
    if args.niveis:
        niveis = [int(n.strip()) for n in args.niveis.split(",")]
    else:
        niveis = sorted(set([1, min(2, len(todos_bicos)), len(todos_bicos)]))

    print(f"Servidor: {args.url}")
    print(f"Bicos disponíveis: {todos_bicos}")
    print(f"Níveis de concorrência a testar: {niveis}")
    print(f"Repetições por nível: {args.repeticoes}\n")

    linha_base_tentativas = None
    for nivel in niveis:
        if nivel > len(todos_bicos):
            print(f"[{nivel} simultâneas] pulado — só há {len(todos_bicos)} bico(s) informado(s)")
            continue
        bico_ids = todos_bicos[:nivel]
        print(f"=== {nivel} leitura(s) simultânea(s) — bicos {bico_ids} ===")
        for rep in range(args.repeticoes):
            resultados = rodar_nivel(args.url, bico_ids, args.api_key, args.timeout)
            resumo = resumir(resultados)
            print(f"  rodada {rep + 1}: {resumo}")
            for r in resultados:
                if not r["ok"]:
                    print(f"    bico {r['bico_id']}: FALHOU — {r['erro']}")
            if nivel == 1:
                tentativas = [r["tentativas"] for r in resultados if r["ok"] and r.get("tentativas") is not None]
                if tentativas and linha_base_tentativas is None:
                    linha_base_tentativas = tentativas[0]
        print()

    if linha_base_tentativas:
        print(f"Referência (1 leitura sozinha): {linha_base_tentativas} tentativas.")
        print("Compare com a média de tentativas nos níveis mais altos — queda grande")
        print("ali indica que o servidor não aguenta bem essa quantidade de leituras juntas.")


if __name__ == "__main__":
    main()
