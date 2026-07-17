"""Ponto de entrada — chama servidor.iniciar()."""
import argparse
import warnings

warnings.filterwarnings("ignore", message=".*pin_memory.*no accelerator.*", category=UserWarning)

import servidor

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ALPR — Leitura de Placas")
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Reinicia automaticamente ao detectar alterações em .py ou .html",
    )
    args = parser.parse_args()
    servidor.iniciar(reload=args.reload)
