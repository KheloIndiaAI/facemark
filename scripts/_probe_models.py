"""Dev tool: probe ONNX model input/output shapes. Usage: _probe_models.py [names...]"""
import sys
from pathlib import Path

import onnxruntime as ort

MODELS_DIR = Path(__file__).resolve().parent.parent / "data" / "models"

DEFAULTS = ["adaface_ir101", "gfpgan_v1.4", "genderage", "det_10g"]


def probe(name: str) -> None:
    p = MODELS_DIR / f"{name}.onnx"
    if not p.exists():
        print(f"=== {name}: FILE MISSING ({p})")
        return
    try:
        s = ort.InferenceSession(str(p), providers=["CPUExecutionProvider"])
        print(f"=== {name} ===")
        for i in s.get_inputs():
            print(f"  IN  {i.name} {i.shape} {i.type}")
        for o in s.get_outputs():
            print(f"  OUT {o.name} {o.shape} {o.type}")
    except Exception as e:
        print(f"=== {name} FAILED: {type(e).__name__}: {e}")


if __name__ == "__main__":
    for n in (sys.argv[1:] or DEFAULTS):
        probe(n)
