#!/usr/bin/env bash
# Run explicitly inside robotwin after its Python 3.10 torch installation.
# No baseline/policy extras, XPolicyLab installation, or source patches are run.
set -euo pipefail

export PATH="/opt/conda/envs/robotwin/bin:/opt/conda/bin:${PATH}"
export CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"
export CUDA_PATH="${CUDA_HOME}"
export PATH="${CUDA_HOME}/bin:${PATH}"
export TORCH_CUDA_ARCH_LIST="8.9"
export MAX_JOBS="${MAX_JOBS:-2}"
if [[ -d /usr/local/cuda/include/cub ]]; then
  export CUB_HOME="/usr/local/cuda/include"
fi

CUROBO_SOURCE="${CUROBO_SOURCE:-/opt/robotwin-build/curobo}"
PYTORCH3D_SOURCE="${PYTORCH3D_SOURCE:-/opt/robotwin-build/pytorch3d}"
RUN_DIRECTORY="${CUDA_EXTENSIONS_RUN_DIRECTORY:-/workspace/diagnostics/cuda_extensions}"
export CUROBO_SOURCE PYTORCH3D_SOURCE RUN_DIRECTORY

[[ -x "${CUDA_HOME}/bin/nvcc" ]] || { echo 'CUDA nvcc was not found' >&2; exit 2; }
[[ -f "${CUROBO_SOURCE}/setup.cfg" && -f "${PYTORCH3D_SOURCE}/setup.py" ]] || {
  echo 'The pinned curobo/PyTorch3D source checkouts are required' >&2; exit 2;
}
mkdir -p "${RUN_DIRECTORY}"

# Preflight uses metadata/AST/configparser, without executing either setup.py.
# Freeze every existing distribution (apart from deliberate build-tool/warp
# pins and the two source projects) to prevent transitive upgrades to the core.
python - <<'PY'
import ast
import configparser
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys

if sys.version_info[:2] != (3, 10):
    raise SystemExit(f'Expected Python 3.10, got {sys.version}')
expected = {
    'numpy': '1.26.4', 'scipy': '1.10.1',
    'torch': '2.4.1+cu121', 'torchvision': '0.19.1+cu121',
}
for name, version in expected.items():
    actual = metadata.version(name)
    if actual != version:
        raise SystemExit(f'Refusing to change the core: {name}={actual}, expected {version}')

sources = {
    'curobo': (Path(os.environ['CUROBO_SOURCE']), 'd64c4b005459db10c5dd867d8b30a87d5bda9bdb'),
    'pytorch3d': (Path(os.environ['PYTORCH3D_SOURCE']), '75ebeeaea0908c5527e7b1e305fbc7681382db47'),
}
for name, (path, expected_commit) in sources.items():
    actual = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != expected_commit:
        raise SystemExit(f'{name}: source revision {actual} differs from {expected_commit}')
    # The installer relies on the inspected dependency and extension definitions.
    subprocess.run(['git', '-C', str(path), 'diff', '--exit-code', 'HEAD', '--',
                    'setup.py', 'setup.cfg', 'pyproject.toml'], check=True)

config = configparser.ConfigParser()
config.read(sources['curobo'][0] / 'setup.cfg')
curobo_requirements = [line.strip() for line in config['options']['install_requires'].splitlines()
                       if line.strip()]
tree = ast.parse((sources['pytorch3d'][0] / 'setup.py').read_text())
requirement_nodes = [keyword.value for node in ast.walk(tree) if isinstance(node, ast.Call)
                     for keyword in node.keywords if keyword.arg == 'install_requires']
if len(requirement_nodes) != 1:
    raise SystemExit('Could not identify the inspected PyTorch3D runtime dependencies')
pytorch3d_requirements = ast.literal_eval(requirement_nodes[0])
if pytorch3d_requirements != ['iopath']:
    raise SystemExit('PyTorch3D runtime requirements differ from the inspected v0.7.8 source')

normalize = lambda name: re.sub(r'[-_.]+', '-', name).lower()
pins = {**expected, 'setuptools': '69.5.1', 'warp-lang': '1.12.0',
        'setuptools-scm': '8.1.0', 'pybind11': '2.13.6'}
exclude = {'nvidia-curobo', 'pytorch3d', *pins}
snapshot = {}
for dist in metadata.distributions():
    name = dist.metadata.get('Name')
    if name and normalize(name) not in exclude:
        name = normalize(name)
        if name in snapshot and snapshot[name] != dist.version:
            raise SystemExit(f'Ambiguous installed distribution: {name}')
        snapshot[name] = dist.version
constraints = {**snapshot, **pins}
run = Path(os.environ['RUN_DIRECTORY'])
(run / 'constraints.txt').write_text(''.join(f'{name}=={version}\n'
                                           for name, version in sorted(constraints.items())))
requirements = list(dict.fromkeys(curobo_requirements + pytorch3d_requirements + [
    'setuptools==69.5.1', 'setuptools-scm==8.1.0', 'pybind11==2.13.6',
    'warp-lang==1.12.0', 'ninja',
]))
(run / 'runtime-requirements.txt').write_text('\n'.join(requirements) + '\n')
(run / 'plan.json').write_text(json.dumps({
    'python': sys.version, 'core_versions': expected,
    'source_revisions': {name: commit for name, (_, commit) in sources.items()},
    'source_paths': {name: str(path) for name, (path, _) in sources.items()},
    'curobo_runtime_requirements': curobo_requirements,
    'pytorch3d_runtime_requirements': pytorch3d_requirements,
    'frozen_distributions': len(snapshot), 'explicit_pins': pins,
    'cuda_home': os.environ['CUDA_HOME'], 'cuda_arch': os.environ['TORCH_CUDA_ARCH_LIST'],
    'max_jobs': os.environ['MAX_JOBS'], 'cub_home': os.environ.get('CUB_HOME'),
}, indent=2) + '\n')
print(f'Preflight passed; core and {len(snapshot)} other installed distributions constrained')
PY

export PIP_CONSTRAINT="${RUN_DIRECTORY}/constraints.txt"
python -m pip install --disable-pip-version-check \
  --constraint "${PIP_CONSTRAINT}" \
  --requirement "${RUN_DIRECTORY}/runtime-requirements.txt"

# Build the projects sequentially; dependency resolution is disabled for both.
# An explicit resume flag can reuse this exact checkout's installed CuRobo.
if [[ "${ROBOTWIN_SKIP_CUROBO_BUILD:-0}" == "1" ]]; then
  python - <<'PY'
import importlib.machinery
import importlib.metadata as metadata
import json
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

distribution = metadata.distribution('nvidia-curobo')
if distribution.version != '0.7.8':
    raise SystemExit(f'Cannot skip CuRobo build: installed version is {distribution.version}')
direct_url = json.loads(distribution.read_text('direct_url.json') or '{}')
url = urlsplit(direct_url.get('url', ''))
source = Path(os.environ['CUROBO_SOURCE']).resolve()
if (url.scheme != 'file' or url.netloc not in ('', 'localhost')
        or Path(unquote(url.path)).resolve() != source
        or direct_url.get('dir_info', {}).get('editable') is not True):
    raise SystemExit('Cannot skip CuRobo build: editable source path does not match CUROBO_SOURCE')
library_directory = source / 'src' / 'curobo' / 'curobolib'
for name in ('lbfgs_step_cu', 'kinematics_fused_cu', 'line_search_cu', 'tensor_step_cu', 'geom_cu'):
    candidates = [library_directory / (name + suffix)
                  for suffix in importlib.machinery.EXTENSION_SUFFIXES]
    if not any(path.is_file() and path.stat().st_size > 0 for path in candidates):
        raise SystemExit(f'Cannot skip CuRobo build: native library missing: {name}')
print('Reusing installed CuRobo 0.7.8: source path and all five native libraries verified')
PY
else
  python -m pip install --disable-pip-version-check \
    --constraint "${PIP_CONSTRAINT}" --no-build-isolation --no-deps \
    --editable "${CUROBO_SOURCE}"
fi

# Keep the successful normal (non-editable) PyTorch3D wheel for reuse.
WHEEL_DIRECTORY="${RUN_DIRECTORY}/wheelhouse-pytorch3d-cp310-cu121-sm89"
mkdir -p "${WHEEL_DIRECTORY}"
FORCE_CUDA=1 PYTORCH3D_FORCE_NO_CUDA=0 PYTORCH3D_NO_EXTENSION=0 \
  python -m pip wheel --disable-pip-version-check \
  --constraint "${PIP_CONSTRAINT}" --no-build-isolation --no-deps \
  --wheel-dir "${WHEEL_DIRECTORY}" "${PYTORCH3D_SOURCE}"
shopt -s nullglob
pytorch3d_wheels=("${WHEEL_DIRECTORY}"/pytorch3d-0.7.8-cp310-cp310-*.whl)
[[ ${#pytorch3d_wheels[@]} -eq 1 ]] || {
  echo 'Expected one PyTorch3D 0.7.8 CPython 3.10 wheel' >&2; exit 2;
}
python -m pip install --disable-pip-version-check \
  --constraint "${PIP_CONSTRAINT}" --no-build-isolation --no-deps \
  "${pytorch3d_wheels[0]}"

# Report installed metadata and ensure the protected versions did not move.
python - <<'PY'
import importlib.metadata as metadata
import json
import os
from pathlib import Path

expected = {'numpy': '1.26.4', 'scipy': '1.10.1', 'torch': '2.4.1+cu121',
            'torchvision': '0.19.1+cu121', 'setuptools': '69.5.1', 'warp-lang': '1.12.0'}
for name, version in expected.items():
    actual = metadata.version(name)
    if actual != version:
        raise SystemExit(f'Protected dependency changed: {name}={actual}, expected {version}')
report = {name: metadata.version(name) for name in (
    *expected, 'nvidia-curobo', 'pytorch3d', 'setuptools-scm', 'pybind11', 'ninja',
)}
destination = Path(os.environ['RUN_DIRECTORY']) / 'installed.json'
destination.write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
print(f'Installation metadata: {destination}')
PY
