#!/usr/bin/env bash
set -euo pipefail
export PATH="/opt/conda/envs/robotwin/bin:/opt/conda/bin:$PATH"
python - <<'PY'
from pathlib import Path
source = Path('/workspace/RoboTwin/scripts/requirements.txt').read_text()
lines = [line for line in source.splitlines()
         if not line.strip().startswith(('torch==', 'torchvision', 'sapien=='))]
Path('/workspace/requirements-core.txt').write_text('\n'.join(lines) + '\n')
PY
python -m pip install --disable-pip-version-check -r /workspace/requirements-core.txt
