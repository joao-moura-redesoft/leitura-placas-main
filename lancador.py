"""Lançador do servidor ALPR — vira o ALPR.exe (PyInstaller --onefile).

Não empacota a aplicação: é um binário fino que só

  1. descobre a pasta onde ele mesmo está (a raiz do projeto),
  2. entra nela  — assim os caminhos relativos do servidor
     (app/web/templates, app/web/static, config.txt, placas.db, hls/, testes/…)
     resolvem igual a rodar `python -m app.main` na mão,
  3. sobe o servidor usando o Python do .venv que vem junto na pasta,
  4. abre o navegador na tela quando a porta responder.

Distribuível = a pasta inteira do projeto (com .venv e models/ dentro) + este .exe
na raiz. Não é um arquivo único; é um atalho robusto que não depende do
diretório de onde foi chamado nem de Python instalado no sistema.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path


def _raiz() -> Path:
    # Congelado (--onefile): sys.executable é o próprio ALPR.exe, que mora na raiz.
    # Rodando como .py (dev): a raiz é a pasta deste arquivo.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _python_do_venv(raiz: Path) -> Path | None:
    for rel in (
        Path(".venv") / "Scripts" / "python.exe",   # Windows
        Path(".venv") / "bin" / "python",           # Linux/mac (caso rode como .py)
    ):
        cand = raiz / rel
        if cand.is_file():
            return cand
    return None


def _ler_porta(raiz: Path) -> int:
    cfg = raiz / "config.txt"
    if cfg.is_file():
        for linha in cfg.read_text(encoding="utf-8", errors="ignore").splitlines():
            chave, _, valor = linha.partition("=")
            if chave.strip() == "porta" and valor.strip().isdigit():
                return int(valor.strip())
    return 14000


def _abrir_navegador_quando_subir(porta: int) -> None:
    alvo = f"http://localhost:{porta}"
    for _ in range(120):                     # ~2 min de tolerância (carga de modelo no 1º boot)
        time.sleep(1)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", porta)) == 0:
                break
    else:
        return
    try:
        webbrowser.open(alvo)
    except Exception:
        pass


def _pausar_e_sair(codigo: int) -> None:
    # Sem isto, um erro fecha a janela na cara de quem deu duplo-clique.
    try:
        input("\nPressione ENTER para fechar...")
    except EOFError:
        pass
    sys.exit(codigo)


def main() -> None:
    raiz = _raiz()
    os.chdir(raiz)

    py = _python_do_venv(raiz)
    if py is None:
        print("=" * 64)
        print("  ERRO: não encontrei o Python do ambiente virtual.")
        print(f"  Esperado: {raiz / '.venv' / 'Scripts' / 'python.exe'}")
        print("  Coloque o ALPR.exe na RAIZ do projeto (junto da pasta .venv).")
        print("=" * 64)
        _pausar_e_sair(1)

    if not (raiz / "app" / "main.py").is_file():
        print("=" * 64)
        print("  ERRO: não achei app/main.py ao lado do executável.")
        print(f"  Pasta atual: {raiz}")
        print("=" * 64)
        _pausar_e_sair(1)

    porta = _ler_porta(raiz)
    threading.Thread(
        target=_abrir_navegador_quando_subir, args=(porta,), daemon=True
    ).start()

    print("=" * 64)
    print("  Leitura de Placas (ALPR) — lançador")
    print(f"  Pasta:  {raiz}")
    print(f"  Python: {py}")
    print(f"  URL:    http://localhost:{porta}  (o navegador abre sozinho)")
    print("  Para encerrar: feche esta janela ou Ctrl+C")
    print("=" * 64, flush=True)

    try:
        codigo = subprocess.call([str(py), "-m", "app.main"], cwd=str(raiz))
    except KeyboardInterrupt:
        codigo = 0
    if codigo != 0:
        _pausar_e_sair(codigo)


if __name__ == "__main__":
    main()
