"""Check the pinned PyTorch base and a small CUDA operation."""

import json
from pathlib import Path
import sys

import torch


def main():
    report = {
        "python": sys.version,
        "pytorch": torch.__version__,
        "pytorch_cuda": torch.version.cuda,
        "cuda_available": torch.cuda.is_available(),
        "checks": {
            "pytorch_2_4_1": torch.__version__.split("+")[0] == "2.4.1",
            "cuda_12_1_build": torch.version.cuda == "12.1",
        },
    }
    if report["cuda_available"]:
        report["gpu"] = torch.cuda.get_device_name(0)
        actual = (torch.arange(4, dtype=torch.float32, device="cuda") * 2).tolist()
        report["cuda_result"] = actual
        report["checks"]["cuda_operation"] = actual == [0.0, 2.0, 4.0, 6.0]
    else:
        report["checks"]["cuda_operation"] = False
    report["passed"] = all(report["checks"].values())
    output = Path("diagnostics/base-environment.json")
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
