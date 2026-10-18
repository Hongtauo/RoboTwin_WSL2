#!/usr/bin/env bash
set -euo pipefail
export PATH="/opt/conda/envs/robotwin/bin:/opt/conda/bin:$PATH"
export CUDA_PATH=/usr/local/cuda
export CUDACXX=/usr/local/cuda/bin/nvcc
export CUDAARCHS=89
export CMAKE_GENERATOR=Ninja
export CMAKE_BUILD_PARALLEL_LEVEL="${ROBOTWIN_BUILD_JOBS:-2}"
export MAX_JOBS="${ROBOTWIN_BUILD_JOBS:-2}"
cd /opt/robotwin-build/SAPIEN
python /opt/robotwin-build/patch_builddeps.py .
python /opt/robotwin-build/patch_pybind11_pin.py .
python /opt/robotwin-build/patch_pinocchio_submodules.py .
python - <<'PY'
from pathlib import Path
path = Path('setup.py')
source = path.read_text()
old = 'version = generate_version()\n'
new = 'version = generate_version() + "+wsldzn"\n'
if old in source:
    assert source.count(old) == 1
    path.write_text(source.replace(old, new))
else:
    assert new in source
PY
build_options=()
if [[ -f sapien_build/_sapien_install/lib/libsapien.so ]]; then
  build_options+=(--pybind-only)
fi
python -u setup.py "${build_options[@]}" bdist_wheel
python -m pip install --no-deps --force-reinstall dist/sapien-*+wsldzn-*.whl
python /opt/robotwin-build/patch_robotwin_optional.py --components sapien --apply
mkdir -p /workspace/diagnostics/wheels
cp dist/sapien-*+wsldzn-*.whl /workspace/diagnostics/wheels/
python /workspace/check_sapien.py physics
python /workspace/check_sapien.py render
