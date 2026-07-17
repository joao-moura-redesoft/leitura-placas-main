"""
Baixa imagens reais de placas brasileiras da internet (Wikimedia Commons)
para uso como dataset de testes no sistema ALPR.

Fontes: Wikimedia Commons (licença CC BY-SA / CC BY)
Cobre os 4 formatos:
  - mercosul_carro  : fundo branco, faixa azul, padrão AAA0A00
  - mercosul_moto   : fundo branco, faixa azul, 2 linhas, padrão AAA0A00
  - antigo_carro    : fundo branco, sem faixa, padrão AAA0000
  - antigo_moto     : faixa metálica cinza, 2 linhas, padrão AAA0000
"""

import json
import os
import re
import hashlib
import urllib.request
import urllib.error
import time

# ─── Configurações ────────────────────────────────────────────────────────────
FOTOS_DIR = os.path.join(os.path.dirname(__file__), "fotos")
DATASET_PATH = os.path.join(os.path.dirname(__file__), "dataset.json")

# Cabeçalho para evitar bloqueio por user-agent
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; ALPR-Dataset-Builder/1.0; "
        "+https://github.com/leitura-placas) "
        "urllib/3.x"
    )
}

# ─── Lista de imagens a baixar ─────────────────────────────────────────────────
# Cada entrada: (url, nome_destino, formato, tipo_veiculo, placa_texto, obs)
IMAGENS = [
    # ── MERCOSUL CARRO ──────────────────────────────────────────────────────
    (
        "https://upload.wikimedia.org/wikipedia/commons/1/17/"
        "Brazilian_vehicle_license_plate_%282018-%29.jpg",
        "real_mercosul_carro_1.jpg",
        "mercosul",
        "mercosul_carro",
        "LSN4149",
        "placa mercosul carro particular RJ (foto real 2018) — CC BY 4.0",
    ),
    (
        "https://upload.wikimedia.org/wikipedia/commons/3/3c/"
        "Placa_de_ve%C3%ADculo_Brasil_2018_A0B1CD2_Mercosul.jpg",
        "real_mercosul_carro_2.jpg",
        "mercosul",
        "mercosul_carro",
        "A0B1D2",  # placa didática/exemplo
        "placa mercosul carro exemplo didático A0B1CD2 — CC BY-SA 4.0",
    ),
    (
        "https://upload.wikimedia.org/wikipedia/commons/0/04/"
        "Placas_Mercosul_2020.jpg",
        "real_mercosul_carro_3.jpg",
        "mercosul",
        "mercosul_carro",
        None,  # imagem mostra múltiplos tipos, placa não identificável isolada
        "quadro comparativo placas Mercosul 2020 (preto/vermelho/azul/ouro) — CC BY-SA 4.0",
    ),
    (
        "https://upload.wikimedia.org/wikipedia/commons/a/ac/"
        "License_plate_of_Brazil_01.jpg",
        "real_mercosul_carro_4.jpg",
        "mercosul",
        "mercosul_carro",
        None,
        "License plate of Brazil 01 — Wikimedia Commons",
    ),
    (
        "https://upload.wikimedia.org/wikipedia/commons/c/c6/"
        "License_plate_of_Brazil_02.jpg",
        "real_mercosul_carro_5.jpg",
        "mercosul",
        "mercosul_carro",
        None,
        "License plate of Brazil 02 — Wikimedia Commons",
    ),
    (
        "https://upload.wikimedia.org/wikipedia/commons/1/1a/"
        "License_plate_of_Brazil_03.jpg",
        "real_mercosul_carro_6.jpg",
        "mercosul",
        "mercosul_carro",
        None,
        "License plate of Brazil 03 — Wikimedia Commons",
    ),
    (
        "https://upload.wikimedia.org/wikipedia/commons/4/44/"
        "License_plate_of_Brazil_04.jpg",
        "real_mercosul_carro_7.jpg",
        "mercosul",
        "mercosul_carro",
        None,
        "License plate of Brazil 04 — Wikimedia Commons",
    ),
    # ── MERCOSUL MOTO ───────────────────────────────────────────────────────
    (
        "https://upload.wikimedia.org/wikipedia/commons/b/be/"
        "Placas_de_ve%C3%ADculos_do_Mercosul_mercosur.jpg",
        "real_mercosul_moto_1.jpg",
        "mercosul",
        "mercosul_moto",
        None,
        "quadro comparativo placas Mercosul países membros (inclui moto) — CC BY-SA 4.0",
    ),
    # ── ANTIGO CARRO ────────────────────────────────────────────────────────
    (
        "https://upload.wikimedia.org/wikipedia/commons/c/c7/"
        "Passeio_de_ve%C3%ADculos_no_Brasil_2011.jpg",
        "real_antigo_carro_1.jpg",
        "antigo",
        "antigo_carro",
        None,
        "placa padrão passeio Brasil 2011 (antigo AAA0000) — Wikimedia Commons",
    ),
    (
        "https://upload.wikimedia.org/wikipedia/commons/5/53/"
        "Placas_de_ve%C3%ADculos_no_Brasil_2009.jpg",
        "real_antigo_carro_2.jpg",
        "antigo",
        "antigo_carro",
        None,
        "quadro comparativo placas Brasil 2009 (antigo padrão) — Wikimedia Commons",
    ),
    (
        "https://upload.wikimedia.org/wikipedia/commons/3/31/"
        "Placa_passeio_de_ve%C3%ADculos_no_Brasil_2008.png",
        "real_antigo_carro_3.png",
        "antigo",
        "antigo_carro",
        None,
        "placa passeio Brasil 2008 (antigo AAA0000, fonte obrigatória) — Wikimedia Commons",
    ),
    (
        "https://upload.wikimedia.org/wikipedia/commons/d/d6/"
        "1%C2%B0_placa_cinza_do_Brasil.jpg",
        "real_antigo_carro_4.jpg",
        "antigo",
        "antigo_carro",
        "AAA0001",
        "1ª placa cinza do Brasil (AAA-0001) — Wikimedia Commons",
    ),
    (
        "https://upload.wikimedia.org/wikipedia/commons/8/86/"
        "Placa_de_ve%C3%ADculo_Brasil_1990_Mato_Grosso_do_Sul-"
        "Campo_Grande_HQW-5678_atr%C3%A1s.png",
        "real_antigo_carro_5.png",
        "antigo",
        "antigo_carro",
        "HQW5678",
        "placa antigo carro Brasil 1990 MS-Campo Grande HQW-5678 — Wikimedia Commons",
    ),
    # ── ANTIGO MOTO ─────────────────────────────────────────────────────────
    (
        "https://upload.wikimedia.org/wikipedia/commons/8/8f/"
        "Placa_de_ve%C3%ADculo_Brasil_2011_Amazonas-S%C3%A3o_"
        "Sebasti%C3%A3o_do_Uatum%C3%A3_NOP_4567_motocicleta.jpg",
        "real_antigo_moto_1.jpg",
        "antigo",
        "antigo_moto",
        "NOP4567",
        "placa antigo moto Brasil 2011 AM-São Sebastião do Uatumã NOP-4567 — CC BY-SA 3.0",
    ),
]


# ─── Funções auxiliares ────────────────────────────────────────────────────────

def gerar_id(arquivo: str) -> str:
    return hashlib.md5(arquivo.encode()).hexdigest()[:8]


def ja_existe_no_dataset(dataset: dict, arquivo_rel: str) -> bool:
    return any(f["arquivo"] == arquivo_rel for f in dataset.get("fotos", []))


def baixar_imagem(url: str, destino: str) -> bool:
    """Baixa uma imagem via urllib. Retorna True se bem-sucedido."""
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            dados = resp.read()
        with open(destino, "wb") as f:
            f.write(dados)
        tamanho_kb = len(dados) / 1024
        print(f"  OK  {os.path.basename(destino)}  ({tamanho_kb:.1f} KB)")
        return True
    except urllib.error.HTTPError as e:
        print(f"  ERRO HTTP {e.code}: {url}")
        return False
    except urllib.error.URLError as e:
        print(f"  ERRO URL: {e.reason}  →  {url}")
        return False
    except Exception as e:
        print(f"  ERRO inesperado: {e}  →  {url}")
        return False


def inferir_placa_do_nome(nome_arquivo: str) -> str | None:
    """Tenta extrair texto de placa do nome do arquivo de destino."""
    # Já especificado na lista de imagens — retorna None para deixar ao chamador
    return None


def detectar_formato_por_placa(placa: str | None, tipo_veiculo: str) -> str:
    """Retorna 'mercosul' ou 'antigo' com base no tipo de veículo declarado."""
    if "mercosul" in tipo_veiculo:
        return "mercosul"
    return "antigo"


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    os.makedirs(FOTOS_DIR, exist_ok=True)

    # Carrega dataset existente
    if os.path.exists(DATASET_PATH):
        with open(DATASET_PATH, "r", encoding="utf-8") as f:
            dataset = json.load(f)
    else:
        dataset = {"version": 1, "fotos": []}

    baixadas = 0
    identificadas = 0
    adicionadas_dataset = 0
    arquivos_salvos = []

    print(f"\n{'='*60}")
    print("  Baixando imagens reais de placas brasileiras")
    print(f"{'='*60}\n")

    for url, nome, formato, tipo_veiculo, placa_texto, obs in IMAGENS:
        destino = os.path.join(FOTOS_DIR, nome)
        arquivo_rel = f"testes/fotos/{nome}"

        print(f"[{tipo_veiculo}] {nome}")

        # Verifica se já foi baixado
        if os.path.exists(destino):
            print(f"  SKIP  já existe: {destino}")
            baixadas += 1
            if placa_texto:
                identificadas += 1
            arquivos_salvos.append((nome, tipo_veiculo, placa_texto))
        else:
            ok = baixar_imagem(url, destino)
            if ok:
                baixadas += 1
                if placa_texto:
                    identificadas += 1
                arquivos_salvos.append((nome, tipo_veiculo, placa_texto))
            time.sleep(0.5)  # pausa educada entre requisições

        # Adiciona ao dataset se identificada e ainda não presente
        if placa_texto and os.path.exists(destino) and not ja_existe_no_dataset(dataset, arquivo_rel):
            entrada = {
                "id": gerar_id(arquivo_rel),
                "arquivo": arquivo_rel,
                "placa_correta": placa_texto,
                "formato": formato,
                "tipo": "real",
                "obs": obs,
            }
            dataset["fotos"].append(entrada)
            adicionadas_dataset += 1
            print(f"  >> dataset: {placa_texto} [{formato}]")

    # Salva dataset atualizado
    with open(DATASET_PATH, "w", encoding="utf-8") as f:
        json.dump(dataset, f, ensure_ascii=False, indent=2)

    # ─── Relatório final ──────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("  RELATÓRIO FINAL")
    print(f"{'='*60}")
    print(f"  Imagens baixadas/existentes : {baixadas}/{len(IMAGENS)}")
    print(f"  Placas identificadas        : {identificadas}")
    print(f"  Entradas adicionadas ao DB  : {adicionadas_dataset}")
    print(f"  Total no dataset agora      : {len(dataset['fotos'])}")
    print()
    print(f"  {'Arquivo':<35} {'Tipo':<20} {'Placa'}")
    print(f"  {'-'*35} {'-'*20} {'-'*10}")
    for nome_arq, tipo, placa in arquivos_salvos:
        caminho = os.path.join(FOTOS_DIR, nome_arq)
        existe = "OK " if os.path.exists(caminho) else "NOK"
        placa_str = placa if placa else "(desconhecida)"
        print(f"  [{existe}] {nome_arq:<33} {tipo:<20} {placa_str}")
    print()


if __name__ == "__main__":
    main()
