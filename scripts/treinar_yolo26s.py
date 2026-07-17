"""Fine-tuning do YOLO26s em dataset de placas de veículos.

Dataset padrão (sem cadastro):
  HuggingFace — keremberke/license-plate-object-detection
  ~800 imagens anotadas, download automático

Dataset alternativo (maior, ~24k imagens):
  Roboflow Universe — license-plate-recognition-rxg4e (CC BY 4.0)
  Requer API key gratuita em roboflow.com

Uso:
  python scripts/treinar_yolo26s.py                      # HuggingFace (padrão)
  python scripts/treinar_yolo26s.py --api-key SUA_KEY    # Roboflow (~24k imagens)
  python scripts/treinar_yolo26s.py --epochs 100 --modelo yolo26s

AVISO: Treinamento em CPU demora 8-20h.
Recomendamos Google Colab (GPU grátis, ~30 min):
  scripts/treinar_yolo26s_colab.ipynb
"""
from __future__ import annotations
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

MODELS_DIR  = Path(__file__).resolve().parent.parent / "models"
ONNX_PATH   = MODELS_DIR / "plate_detector.onnx"
RUNS_DIR    = MODELS_DIR / "runs" / "treino_yolo26s"

ROBOFLOW_WORKSPACE = "roboflow-universe-projects"
ROBOFLOW_PROJECT   = "license-plate-recognition-rxg4e"
ROBOFLOW_VERSION   = 4


def _pip(pacote: str) -> None:
    subprocess.check_call([sys.executable, "-m", "pip", "install", pacote, "--quiet"])


def baixar_dataset_hf() -> Path:
    """Baixa dataset do HuggingFace sem necessidade de cadastro."""
    try:
        from datasets import load_dataset  # type: ignore[import-untyped]
    except ImportError:
        print("Instalando datasets...")
        _pip("datasets")
        from datasets import load_dataset  # type: ignore[import-untyped]

    destino = MODELS_DIR / "dataset"
    yaml = destino / "data.yaml"
    if yaml.exists():
        print(f"Dataset já baixado em: {destino}")
        return destino

    print("Baixando dataset do HuggingFace (keremberke/license-plate-object-detection)...")
    ds = load_dataset("keremberke/license-plate-object-detection", name="full")

    for split, nome in [("train", "train"), ("validation", "valid"), ("test", "test")]:
        (destino / nome / "images").mkdir(parents=True, exist_ok=True)
        (destino / nome / "labels").mkdir(parents=True, exist_ok=True)
        subset = ds[split]
        for i, sample in enumerate(subset):
            img_path = destino / nome / "images" / f"{i:06d}.jpg"
            sample["image"].save(str(img_path))
            W, H = sample["image"].size
            lines = []
            for obj in sample["objects"]:
                x1, y1, x2, y2 = obj["bbox"]
                cx = ((x1 + x2) / 2) / W
                cy = ((y1 + y2) / 2) / H
                w  = (x2 - x1) / W
                h  = (y2 - y1) / H
                lines.append(f"0 {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
            (destino / nome / "labels" / f"{i:06d}.txt").write_text("\n".join(lines))
        print(f"  {nome}: {len(subset)} imagens")

    (destino / "data.yaml").write_text(
        f"path: {destino}\ntrain: train/images\nval: valid/images\n"
        f"test: test/images\nnc: 1\nnames: ['license-plate']\n"
    )
    print(f"Dataset salvo em: {destino}")
    return destino


def baixar_dataset_roboflow(api_key: str) -> Path:
    """Baixa dataset maior do Roboflow (~24k imagens). Requer API key gratuita."""
    try:
        from roboflow import Roboflow  # type: ignore[import-untyped]
    except ImportError:
        print("Instalando roboflow...")
        _pip("roboflow")
        from roboflow import Roboflow  # type: ignore[import-untyped]

    destino = MODELS_DIR / "dataset"
    if (destino / "data.yaml").exists():
        print(f"Dataset já baixado em: {destino}")
        return destino

    print("Baixando dataset de placas (Roboflow ~24k imagens)...")
    rf = Roboflow(api_key=api_key)
    proj = rf.workspace(ROBOFLOW_WORKSPACE).project(ROBOFLOW_PROJECT)
    ds = proj.version(ROBOFLOW_VERSION).download("yolov8", location=str(destino))
    print(f"Dataset salvo em: {ds.location}")
    return Path(ds.location)


def treinar(modelo: str, epochs: int, batch: int, device: str, dataset_path: Path) -> Path | None:
    from ultralytics import YOLO

    print(f"\n{'='*60}")
    print(f"Treinando {modelo}.pt por {epochs} epochs")
    print(f"Dataset: {dataset_path}")
    print(f"Device: {device} | Batch: {batch}")
    print(f"{'='*60}\n")

    model = YOLO(f"{modelo}.pt")
    results = model.train(
        data=str(dataset_path / "data.yaml"),
        epochs=epochs,
        imgsz=640,
        batch=batch,
        device=device,
        name="treino_yolo26s",
        project=str(MODELS_DIR / "runs"),
        exist_ok=True,
        # Augmentação recomendada para placas
        hsv_h=0.015,
        hsv_s=0.4,
        hsv_v=0.4,
        degrees=5.0,
        translate=0.1,
        scale=0.3,
        shear=2.0,
        perspective=0.0002,
        flipud=0.0,
        fliplr=0.0,   # placas não são espelhadas
        mosaic=0.5,
        mixup=0.1,
        copy_paste=0.1,
        # Paciência para early stopping
        patience=20,
        # Salva checkpoint a cada 10 epochs
        save_period=10,
        verbose=True,
    )

    best = Path(results.save_dir) / "weights" / "best.pt"
    if not best.exists():
        print("ERRO: best.pt não encontrado após treinamento.")
        return None

    # Copia best.pt para models/
    destino_pt = MODELS_DIR / "plate_detector_yolo26s.pt"
    shutil.copy(best, destino_pt)
    print(f"\nMelhor modelo salvo em: {destino_pt}")
    return destino_pt


def exportar_onnx(pt: Path) -> bool:
    from ultralytics import YOLO

    print(f"\nExportando {pt.name} → ONNX...")
    model = YOLO(str(pt))
    # YOLO26: exporta sem end2end para compatibilidade máxima com onnxruntime
    saida = Path(model.export(format="onnx", imgsz=640, opset=12))

    onnx_novo = MODELS_DIR / "plate_detector_yolo26s.onnx"
    if saida.resolve() != onnx_novo.resolve():
        onnx_novo.unlink(missing_ok=True)
        saida.rename(onnx_novo)

    print(f"Exportado: {onnx_novo} ({onnx_novo.stat().st_size // 1024} KB)")
    print(f"\nPara usar este modelo como padrão:")
    print(f"  copy {onnx_novo} {ONNX_PATH}")
    return True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--api-key", default="",
                   help="API key do Roboflow (opcional — sem key usa HuggingFace)")
    p.add_argument("--epochs",  type=int, default=50)
    p.add_argument("--modelo",  default="yolo26s", choices=["yolo26n", "yolo26s", "yolo26m"])
    p.add_argument("--batch",   type=int, default=0,
                   help="0 = auto (16 GPU / 4 CPU)")
    p.add_argument("--device",  default="",
                   help="cpu / 0 / 0,1 (vazio = auto-detecta GPU)")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    # Auto device/batch
    device = args.device
    if not device:
        try:
            import torch
            device = "0" if torch.cuda.is_available() else "cpu"
        except Exception:
            device = "cpu"

    batch = args.batch
    if batch == 0:
        batch = 16 if device != "cpu" else 4

    print(f"Device detectado: {device} | Batch: {batch}")
    if device == "cpu":
        print("AVISO: Treinamento em CPU pode levar 8-20 horas.")
        print("Recomendamos usar Google Colab (GPU grátis).")
        print("Notebook: scripts/treinar_yolo26s_colab.ipynb")
        resp = input("Continuar em CPU? (s/N): ").strip().lower()
        if resp != "s":
            print("Abortado.")
            return 0

    if args.api_key:
        dataset_path = baixar_dataset_roboflow(args.api_key)
    else:
        print("Sem --api-key: usando dataset HuggingFace (~800 imagens).")
        print("Para dataset maior (~24k imgs): --api-key SUA_KEY_ROBOFLOW")
        dataset_path = baixar_dataset_hf()
    pt = treinar(args.modelo, args.epochs, batch, device, dataset_path)
    if pt is None:
        return 1

    exportar_onnx(pt)

    print("\n" + "="*60)
    print("Fine-tuning concluído!")
    print(f"Para ativar o novo modelo:")
    print(f"  copy models\\plate_detector_yolo26s.onnx models\\plate_detector.onnx")
    print(f"  (depois reinicie o servidor)")
    print("="*60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
