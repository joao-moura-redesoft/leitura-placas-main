"""Baixa modelo YOLO26s pré-treinado em placas brasileiras e exporta para ONNX.

Fonte padrão (recomendada):
  RodrigoRRC/license-plate-br-yolo26s (HuggingFace)
  - YOLO26s fine-tunado no dataset UFPR-ALPR (placas BR reais)
  - mAP@50: 97.04% | mAP@50-95: 81.70%
  - Licença: AGPL-3.0 — uso não-comercial (dataset UFPR-ALPR)

Fontes alternativas:
  --legado   : YOLOv8n genérico (Arijit1080/GitHub), sem fine-tuning em placas BR
  --treinar  : fine-tuning do yolo26n COCO usando dataset Roboflow (requer API key)

Uso:
  python scripts/baixar_modelo.py                          # HuggingFace (padrão)
  python scripts/baixar_modelo.py --legado                 # YOLOv8 legado
  python scripts/baixar_modelo.py --treinar --api-key KEY  # fine-tuning próprio
  python scripts/baixar_modelo.py --modelo yolo26s --treinar --api-key KEY
  python scripts/baixar_modelo.py --veiculo                # detector de veículo (2 estágios)
"""
from __future__ import annotations
import argparse
import subprocess
import sys
import urllib.request
from pathlib import Path

# Modelo HuggingFace — YOLO26s treinado em placas brasileiras
HF_REPO = "RodrigoRRC/license-plate-br-yolo26s"
HF_FILE = "best_placas_v2.pt"

# Modelo legado YOLOv8
MODELO_LEGADO_URL = (
    "https://github.com/Arijit1080/Licence-Plate-Detection-using-YOLO-V8/raw/main/best.pt"
)

# Dataset Roboflow para fine-tuning próprio (licença CC BY 4.0)
ROBOFLOW_WORKSPACE = "roboflow-universe-projects"
ROBOFLOW_PROJECT   = "license-plate-recognition-rxg4e"
ROBOFLOW_VERSION   = 4

# Detector de VEÍCULO (1º estágio da detecção em 2 estágios) — YOLOX-s ONNX,
# OpenCV Model Zoo, licença Apache-2.0, treinado em COCO (car/motorcycle/bus/truck).
VEICULO_HF_REPO = "opencv/object_detection_yolox"
VEICULO_HF_FILE = "object_detection_yolox_2022nov.onnx"

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
PT_PATH    = MODELS_DIR / "plate_detector.pt"
ONNX_PATH  = MODELS_DIR / "plate_detector.onnx"
VEICULO_ONNX_PATH = MODELS_DIR / "vehicle_detector.onnx"


def _pip_install(pacote: str) -> None:
    subprocess.check_call([sys.executable, "-m", "pip", "install", pacote, "--quiet"])


def baixar_hf(repo: str, filename: str, destino: Path) -> bool:
    """Baixa arquivo do HuggingFace Hub. Instala huggingface_hub se necessário."""
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        print("Instalando huggingface_hub...")
        _pip_install("huggingface_hub")
        from huggingface_hub import hf_hub_download

    print(f"Baixando {repo}/{filename} ...")
    destino.parent.mkdir(parents=True, exist_ok=True)
    caminho = hf_hub_download(repo_id=repo, filename=filename, local_dir=str(destino.parent))
    Path(caminho).rename(destino)
    print(f"Salvo em: {destino} ({destino.stat().st_size / 1024:.0f} KB)")
    return True


def baixar_url(url: str, destino: Path) -> None:
    print(f"Baixando: {url}")
    destino.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "leitura-placas/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(destino, "wb") as f:
        total = int(resp.headers.get("Content-Length", 0))
        baixado = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            f.write(chunk)
            baixado += len(chunk)
            if total > 0:
                print(f"  {baixado/1024:.0f}/{total/1024:.0f} KB ({baixado/total*100:.1f}%)", end="\r")
        print()
    print(f"Salvo em: {destino} ({destino.stat().st_size / 1024:.0f} KB)")


def exportar_onnx(pt: Path, onnx: Path, end2end: bool = True) -> bool:
    try:
        from ultralytics import YOLO
    except ImportError:
        print("ERRO: ultralytics não instalado. Execute: pip install ultralytics")
        return False

    modo = "YOLO26 end2end (sem NMS)" if end2end else "YOLOv8 com NMS"
    print(f"Exportando para ONNX [{modo}]...")
    model = YOLO(str(pt))
    kwargs = dict(format="onnx", imgsz=640, opset=12)
    if end2end:
        kwargs["end2end"] = True
    saida = Path(model.export(**kwargs))
    if saida.resolve() != onnx.resolve():
        onnx.unlink(missing_ok=True)
        saida.rename(onnx)
    print(f"Exportado: {onnx} ({onnx.stat().st_size / 1024:.0f} KB)")
    return True


def baixar_dataset_roboflow(api_key: str) -> Path | None:
    try:
        from roboflow import Roboflow  # type: ignore[import-untyped]
    except ImportError:
        print("Instalando roboflow...")
        _pip_install("roboflow")
        from roboflow import Roboflow  # type: ignore[import-untyped]

    print(f"Baixando dataset {ROBOFLOW_PROJECT} v{ROBOFLOW_VERSION}...")
    rf = Roboflow(api_key=api_key)
    dataset = (rf.workspace(ROBOFLOW_WORKSPACE)
                 .project(ROBOFLOW_PROJECT)
                 .version(ROBOFLOW_VERSION)
                 .download("yolov8", location=str(MODELS_DIR / "dataset")))
    return Path(dataset.location)


def fine_tunar(modelo_base: str, epochs: int, api_key: str) -> Path | None:
    dataset_path = baixar_dataset_roboflow(api_key)
    if not dataset_path:
        return None

    try:
        from ultralytics import YOLO
    except ImportError:
        print("ERRO: ultralytics não instalado.")
        return None

    print(f"\nFine-tuning {modelo_base}.pt por {epochs} epochs...")
    model = YOLO(f"{modelo_base}.pt")
    results = model.train(
        data=str(dataset_path / "data.yaml"),
        epochs=epochs,
        imgsz=640,
        batch=16,
        name="plate_detector",
        project=str(MODELS_DIR / "runs"),
        exist_ok=True,
        verbose=False,
    )
    best = Path(results.save_dir) / "weights" / "best.pt"
    if best.exists():
        print(f"Treinamento concluído: {best}")
        return best
    print("ERRO: best.pt não encontrado após treinamento.")
    return None


def fluxo_hf() -> int:
    """Padrão: baixa YOLO26s BR do HuggingFace e exporta para ONNX."""
    if ONNX_PATH.exists():
        print(f"{ONNX_PATH} já existe. Apague para re-baixar.")
        return 0
    if not PT_PATH.exists():
        ok = baixar_hf(HF_REPO, HF_FILE, PT_PATH)
        if not ok:
            return 1
    ok = exportar_onnx(PT_PATH, ONNX_PATH, end2end=True)
    return 0 if ok else 2


def fluxo_legado() -> int:
    """Legado: YOLOv8n sem fine-tuning em placas BR."""
    if ONNX_PATH.exists():
        print(f"{ONNX_PATH} já existe. Apague para re-baixar.")
        return 0
    if not PT_PATH.exists():
        try:
            baixar_url(MODELO_LEGADO_URL, PT_PATH)
        except Exception as e:
            print(f"Falha no download: {e}", file=sys.stderr)
            return 1
    ok = exportar_onnx(PT_PATH, ONNX_PATH, end2end=False)
    return 0 if ok else 2


def fluxo_veiculo() -> int:
    """Baixa o detector de VEÍCULO (YOLOX-s ONNX, Apache-2.0) para a detecção em 2 estágios."""
    if VEICULO_ONNX_PATH.exists():
        print(f"{VEICULO_ONNX_PATH} já existe. Apague para re-baixar.")
        return 0
    ok = baixar_hf(VEICULO_HF_REPO, VEICULO_HF_FILE, VEICULO_ONNX_PATH)
    return 0 if ok else 1


def fluxo_treinar(modelo_base: str, epochs: int, api_key: str) -> int:
    """Fine-tuning do yolo26n usando dataset Roboflow."""
    if ONNX_PATH.exists():
        print(f"{ONNX_PATH} já existe. Apague para re-exportar.")
        return 0
    pt = fine_tunar(modelo_base, epochs, api_key)
    if pt is None:
        return 1
    ok = exportar_onnx(pt, ONNX_PATH, end2end=True)
    return 0 if ok else 2


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Baixa modelo YOLO26 para detecção de placas.")
    p.add_argument("--legado", action="store_true",
                   help="Usa modelo YOLOv8 legado (GitHub, sem placas BR)")
    p.add_argument("--treinar", action="store_true",
                   help="Fine-tuning do yolo26n com dataset Roboflow")
    p.add_argument("--api-key", default="",
                   help="API key Roboflow (obrigatório com --treinar)")
    p.add_argument("--epochs", type=int, default=30,
                   help="Epochs de fine-tuning (padrão: 30)")
    p.add_argument("--modelo", default="yolo26n",
                   choices=["yolo26n", "yolo26s", "yolo26m"],
                   help="Base para fine-tuning (padrão: yolo26n)")
    p.add_argument("--veiculo", action="store_true",
                   help="Baixa o detector de VEÍCULO (YOLOX-s, Apache-2.0) para a "
                        "detecção em 2 estágios (veiculo_dois_estagios_* em config.txt)")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    if args.veiculo:
        rc = fluxo_veiculo()
    elif args.legado:
        rc = fluxo_legado()
    elif args.treinar:
        if not args.api_key:
            print("ERRO: --treinar requer --api-key. Crie conta gratuita em roboflow.com.")
            return 1
        rc = fluxo_treinar(args.modelo, args.epochs, args.api_key)
    else:
        rc = fluxo_hf()

    if rc == 0:
        print()
        print("Sucesso! Reinicie o servidor para carregar o novo modelo.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
