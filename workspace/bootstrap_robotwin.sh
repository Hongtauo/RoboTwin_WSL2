#!/usr/bin/env bash
# Execute INSIDE the fresh GPU-enabled PyTorch base container, as root.
# Base: pytorch/pytorch:2.4.1-cuda12.1-cudnn9-devel (Ubuntu 22.04).
# Read-only repository: /opt/robotwin-repro; writable bind: /workspace.
# Linux-native build sources/caches: /opt/robotwin-build.
# This installs packages and runs the local expert once; no policy training,
# cloud inference, or language-generation API is invoked.
set -euo pipefail

REPRO_ROOT="${ROBOTWIN_REPRO_ROOT:-/opt/robotwin-repro}"
WORKSPACE=/workspace
BUILD_ROOT=/opt/robotwin-build
ENV_ROOT=/opt/conda/envs/robotwin
SCRIPTS="${REPRO_ROOT}/workspace"
PATCHES="${REPRO_ROOT}/patches"
REFERENCE_CONSTRAINTS="${REPRO_ROOT}/constraints.txt"
SAPIEN_BUILD_JOBS="${ROBOTWIN_BUILD_JOBS:-${SAPIEN_BUILD_JOBS:-2}}"
CUDA_BUILD_JOBS="${ROBOTWIN_CUDA_JOBS:-${CUDA_BUILD_JOBS:-${MAX_JOBS:-2}}}"
STAGE=preflight
trap 'code=$?; printf "Bootstrap failed in %s at line %s (exit %s)\n" "$STAGE" "$LINENO" "$code" >&2; exit "$code"' ERR

die() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }
stage() { STAGE="$1"; printf '\n== %s ==\n' "$STAGE"; }
[[ "${EUID}" -eq 0 ]] || die 'Run this bootstrap as root inside the setup container.'
[[ "${SAPIEN_BUILD_JOBS}" =~ ^[1-9][0-9]*$ && "${CUDA_BUILD_JOBS}" =~ ^[1-9][0-9]*$ ]] || die 'Build job counts must be positive integers.'
[[ -r "${REFERENCE_CONSTRAINTS}" && -d "${PATCHES}" ]] || die 'The read-only reproduction repository is not mounted at /opt/robotwin-repro.'
[[ -d "${WORKSPACE}" && -w "${WORKSPACE}" ]] || die 'Mount the writable workspace at /workspace.'
[[ -f /usr/lib/wsl/lib/libd3d12.so && -f /usr/lib/wsl/lib/libdxcore.so ]] || die 'Mount the WSL /usr/lib/wsl directory before bootstrapping.'
[[ -x /opt/conda/bin/conda && -x /usr/local/cuda/bin/nvcc ]] || die 'Use the specified PyTorch CUDA devel base image.'

export DEBIAN_FRONTEND=noninteractive
# DrvFs reports the Windows user's UID even for files created by container root.
# Trust only these two pinned checkouts, for this process and its children.
export GIT_CONFIG_COUNT=2
export GIT_CONFIG_KEY_0=safe.directory
export GIT_CONFIG_VALUE_0="${WORKSPACE}/RoboTwin"
export GIT_CONFIG_KEY_1=safe.directory
export GIT_CONFIG_VALUE_1="${WORKSPACE}/RoboTwin/XPolicyLab"
export PYTHONNOUSERSITE=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_CONSTRAINT="${REFERENCE_CONSTRAINTS}"
export CUDA_HOME=/usr/local/cuda
export CUDA_PATH=/usr/local/cuda
export CUDACXX=/usr/local/cuda/bin/nvcc
export CUDAARCHS=89
export TORCH_CUDA_ARCH_LIST=8.9
export CUB_HOME=/usr/local/cuda/include
export SAPIEN_DISABLE_RAY_TRACING=1
export ROBOTWIN_CAMERA_SHADER=default
export VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/dzn_icd.x86_64.json
export VK_DRIVER_FILES="${VK_ICD_FILENAMES}"
export LD_LIBRARY_PATH="/usr/lib/wsl/lib:/usr/local/nvidia/lib:/usr/local/nvidia/lib64${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
mkdir -p "${BUILD_ROOT}" "${WORKSPACE}/cache/torch310" "${WORKSPACE}/diagnostics/wheels"

# The supplied helpers expect their own copies here. Never edit the read-only
# repository or use a Windows bind mount for the native source/build trees.
cp "${PATCHES}"/*.py "${BUILD_ROOT}/"

stage 'system runtime and development dependencies'
apt-get update
apt-get install -y --no-install-recommends \
  git ca-certificates curl gpg unzip zip build-essential pkg-config ffmpeg \
  vulkan-tools libvulkan1 libgl1 libglib2.0-0 \
  libx11-6 libx11-xcb1 libxcb1 libxrandr2 libxinerama1 libxcursor1 libxi6 \
  libx11-dev libxrandr-dev libxinerama-dev libxcursor-dev libxi-dev libxext-dev libgl-dev

stage 'isolated Python 3.10 environment'
if [[ ! -x "${ENV_ROOT}/bin/python" ]]; then
  [[ ! -e "${ENV_ROOT}" ]] || die 'A partial robotwin conda environment exists; inspect it rather than overwriting it.'
  /opt/conda/bin/conda create -y --name robotwin --override-channels -c conda-forge \
    python=3.10.22 pip
fi
export PATH="${ENV_ROOT}/bin:/opt/conda/bin:/usr/local/cuda/bin:${PATH}"
export CONDA_PREFIX="${ENV_ROOT}"
export CONDA_DEFAULT_ENV=robotwin
python - <<'PY'
import sys
if sys.version_info[:3] != (3, 10, 22):
    raise SystemExit(f'Expected Python 3.10.22, found {sys.version}')
PY
python -m pip install --constraint "${REFERENCE_CONSTRAINTS}" pip==26.2.1
python -m pip install --constraint "${REFERENCE_CONSTRAINTS}" \
  setuptools==69.5.1 wheel numpy==1.26.4 scipy==1.10.1 \
  pillow requests transforms3d==0.4.2 lxml networkx pyperclip \
  opencv-python==4.10.0.84 ninja==1.13.2
# This base contains CMake 3.30.3; Ubuntu jammy's apt CMake 3.22 is too old.
if ! cmake --version 2>/dev/null | head -n 1 | grep -qx 'cmake version 3.30.3'; then
  python -m pip install --constraint "${REFERENCE_CONSTRAINTS}" cmake==3.30.3
fi
cmake --version

stage 'pinned source checkouts'
clone_pinned() {
  local url="$1" directory="$2" commit="$3"
  if [[ -e "${directory}" ]]; then
    [[ -d "${directory}/.git" || -f "${directory}/.git" ]] || die "Existing source is not a Git checkout: ${directory}"
    [[ "$(git -C "${directory}" rev-parse --show-toplevel)" == "${directory}" ]] || die "Source root mismatch: ${directory}"
    [[ "$(git -C "${directory}" rev-parse HEAD)" == "${commit}" ]] || die "Existing checkout has a different commit: ${directory}"
  else
    mkdir -p "${directory}"
    git -C "${directory}" init -q
    git -C "${directory}" remote add origin "${url}"
    git -C "${directory}" fetch --depth 1 origin "${commit}"
    git -C "${directory}" checkout --detach FETCH_HEAD
  fi
}
check_submodule() {
  local parent="$1" relative="$2" commit="$3"
  [[ "$(git -C "${parent}" rev-parse "HEAD:${relative}")" == "${commit}" ]] || die "Unexpected pinned gitlink: ${relative}"
  if [[ -e "${parent}/${relative}/.git" ]]; then
    [[ "$(git -C "${parent}/${relative}" rev-parse HEAD)" == "${commit}" ]] || die "Existing submodule has a different commit: ${relative}"
  fi
}
clone_pinned https://github.com/RoboTwin-Platform/RoboTwin.git "${WORKSPACE}/RoboTwin" ea8b21121ebb3cd201ff5b3fe361944ac94eda3f
check_submodule "${WORKSPACE}/RoboTwin" XPolicyLab fa431ecd893ee706883e64fe5fe1464ec8cd928d
# Only the shared XPolicyLab checkout is needed. Do not recurse into policy repos.
git -C "${WORKSPACE}/RoboTwin" submodule update --init --depth 1 XPolicyLab
clone_pinned https://github.com/haosulab/SAPIEN.git "${BUILD_ROOT}/SAPIEN" d9031a351c39986e51bd0716bc3b51f9a05070d8
check_submodule "${BUILD_ROOT}/SAPIEN" 3rd_party/sapien-vulkan-2 d764f627e784bcd4fc5a6271d211bea6744270b4
check_submodule "${BUILD_ROOT}/SAPIEN" 3rd_party/simsense 8ab7719390bcb3bfb585368538b4f55bfa0e5456
git -C "${BUILD_ROOT}/SAPIEN" submodule update --init --recursive --depth 1 \
  3rd_party/sapien-vulkan-2 3rd_party/simsense
check_submodule "${BUILD_ROOT}/SAPIEN" 3rd_party/sapien-vulkan-2 d764f627e784bcd4fc5a6271d211bea6744270b4
check_submodule "${BUILD_ROOT}/SAPIEN" 3rd_party/simsense 8ab7719390bcb3bfb585368538b4f55bfa0e5456
[[ "$(git -C "${WORKSPACE}/RoboTwin/XPolicyLab" rev-parse HEAD)" == fa431ecd893ee706883e64fe5fe1464ec8cd928d ]] || die 'XPolicyLab pin mismatch.'
clone_pinned https://github.com/NVlabs/curobo.git "${BUILD_ROOT}/curobo" d64c4b005459db10c5dd867d8b30a87d5bda9bdb
clone_pinned https://github.com/facebookresearch/pytorch3d.git "${BUILD_ROOT}/pytorch3d" 75ebeeaea0908c5527e7b1e305fbc7681382db47
# setup.py obtains CuRobo's package version from this exact release tag.
for release in 'curobo v0.7.8' 'pytorch3d V0.7.8' 'SAPIEN 3.0.0b1'; do
  read -r directory tag <<< "${release}"
  git -C "${BUILD_ROOT}/${directory}" fetch --depth 1 origin "refs/tags/${tag}:refs/tags/${tag}"
  [[ "$(git -C "${BUILD_ROOT}/${directory}" rev-parse "${tag}^{commit}")" == "$(git -C "${BUILD_ROOT}/${directory}" rev-parse HEAD)" ]] || die "Release tag does not match the pinned commit: ${directory}/${tag}"
done
# Re-runs may contain only the tracked changes made by these guarded patches.
# Do not silently compile unrelated local kernel or simulator modifications.
python - <<'PY'
from pathlib import Path
import subprocess

allowed = {
    '/workspace/RoboTwin': {'envs/_base_task.py', 'scripts/test_render.py',
                           'envs/utils/rand_create_cluttered_actor.py'},
    '/workspace/RoboTwin/XPolicyLab': set(),
    '/opt/robotwin-build/curobo': set(),
    '/opt/robotwin-build/pytorch3d': set(),
    '/opt/robotwin-build/SAPIEN': {'CMakeLists.txt', 'setup.py', 'cmake/pybind11.cmake',
        'pinocchio/cmake/pybind11.cmake', 'pinocchio/cmake/pinocchio.cmake',
        'python/py_package/wrapper/urdf_loader.py',
        '3rd_party/sapien-vulkan-2', '3rd_party/simsense'},
    '/opt/robotwin-build/SAPIEN/3rd_party/simsense': {'CMakeLists.txt'},
    '/opt/robotwin-build/SAPIEN/3rd_party/sapien-vulkan-2': {
        'src/core/instance.cpp', 'src/core/device.cpp', 'src/shader/gbuffer.cpp',
        'include/svulkan2/shader/shader_pack_instance.h',
        'src/shader/shader_pack_instance.cpp', 'src/renderer/renderer.cpp'},
}
for directory, expected in allowed.items():
    raw = subprocess.check_output(['git', '-C', directory, 'diff', '--name-only', '-z', 'HEAD'])
    modified = {name.decode() for name in raw.split(b'\0') if name}
    if modified - expected:
        raise SystemExit(f'Unrelated tracked changes in {directory}: {sorted(modified - expected)}')
PY

stage 'reuse only ABI-independent base NVIDIA wheels'
BASE_SITE=$(/opt/conda/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
TARGET_SITE=$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
export BASE_SITE TARGET_SITE BUILD_ROOT
python - <<'PY'
from importlib import metadata
import os
from pathlib import Path
import runpy
import subprocess
import sys

helper = Path(os.environ['BUILD_ROOT']) / 'reuse_cuda_wheels.py'
spec = runpy.run_path(str(helper))
versions = spec['VERSIONS']
source, target = Path(os.environ['BASE_SITE']), Path(os.environ['TARGET_SITE'])
installed = {}
for distribution in metadata.distributions(path=[str(target)]):
    name = spec['canonical'](distribution.metadata.get('Name', ''))
    if name in versions:
        if name in installed:
            raise SystemExit(f'Ambiguous duplicate NVIDIA distribution: {name}')
        installed[name] = distribution
present = {name: distribution.version for name, distribution in installed.items()}
if present:
    if present != versions:
        raise SystemExit('Partial or mismatched NVIDIA distributions exist; refusing to merge/overwrite them')
    for name, version in versions.items():
        distribution = installed[name]
        libraries = [distribution.locate_file(path) for path in distribution.files or ()
                     if '.so' in Path(path).name]
        if not libraries or any(not path.is_file() or path.stat().st_size == 0 for path in libraries):
            raise SystemExit(f'Incomplete reused distribution: {name}')
    print('All twelve exact NVIDIA distributions already present; reuse helper skipped')
else:
    subprocess.run([sys.executable, str(helper), str(source), str(target), '--apply'], check=True)
PY

stage 'CPython 3.10 Torch, torchvision and Triton wheels'
# Download with Python 3.10 itself, so cp311 Torch/Triton binaries are never reused.
if ! python - <<'PY'
from importlib import metadata
expected = {'torch':'2.4.1+cu121', 'torchvision':'0.19.1+cu121', 'triton':'3.0.0'}
try:
    raise SystemExit(0 if all(metadata.version(k) == v for k, v in expected.items()) else 1)
except metadata.PackageNotFoundError:
    raise SystemExit(1)
PY
then
  python -m pip download --only-binary=:all: --no-deps --dest "${WORKSPACE}/cache/torch310" \
    --index-url https://download.pytorch.org/whl/cu121 torch==2.4.1+cu121 torchvision==0.19.1+cu121
  python -m pip download --only-binary=:all: --no-deps --dest "${WORKSPACE}/cache/torch310" \
    --index-url https://pypi.org/simple triton==3.0.0
  # Normal dependency resolution installs only small missing Torch dependencies;
  # the exact NVIDIA packages above already satisfy the large CUDA requirements.
  python -m pip install --constraint "${REFERENCE_CONSTRAINTS}" \
    --find-links "${WORKSPACE}/cache/torch310" \
    --extra-index-url https://download.pytorch.org/whl/cu121 \
    torch==2.4.1+cu121 torchvision==0.19.1+cu121 triton==3.0.0
fi

stage 'pinned RoboTwin core and SAPIEN development dependencies'
bash "${SCRIPTS}/install_robotwin_core.sh"
bash "${SCRIPTS}/prepare_sapien_build.sh"
if [[ "$(dpkg-query -W -f='${Version}' mesa-vulkan-drivers 2>/dev/null || true)" != '25.0.7~kisak3~j' ]] \
  || [[ ! -f "${VK_ICD_FILENAMES}" ]]; then
  bash "${SCRIPTS}/install_dzn.sh"
fi
[[ -f "${VK_ICD_FILENAMES}" && -f /usr/lib/x86_64-linux-gnu/libvulkan_dzn.so ]] || die 'Pinned Mesa DZN installation is incomplete.'

stage 'guarded source patches'
python "${BUILD_ROOT}/patch_robotwin_raster.py" "${WORKSPACE}/RoboTwin" --apply
python "${BUILD_ROOT}/patch_lazy_clutter.py" "${WORKSPACE}/RoboTwin" --apply
python "${BUILD_ROOT}/patch_builddeps.py" "${BUILD_ROOT}/SAPIEN" --cuda-arch 89
python "${BUILD_ROOT}/patch_pybind11_pin.py" "${BUILD_ROOT}/SAPIEN"
python "${BUILD_ROOT}/patch_pinocchio_submodules.py" "${BUILD_ROOT}/SAPIEN"
# The original static-cull helper requires pristine sources. Reconstruct its
# exact expected output from the pinned Git objects to recognize a prior run.
python - <<'PY'
import os
from pathlib import Path
import runpy
import subprocess
import sys

root = Path(os.environ['BUILD_ROOT'])
source = root / 'SAPIEN' / '3rd_party' / 'sapien-vulkan-2'
helper = root / 'patch_svulkan2_static_cull.py'
module = runpy.run_path(str(helper))
originals = {path: subprocess.check_output(['git', '-C', str(source), 'show', f'HEAD:{path}']).decode()
             for path in module['SOURCE_BLOBS']}
updated = module['patch_texts'](originals)
current = {path: (source / path).read_text() for path in originals}
if current == updated:
    print('Exact static-cull patch already applied')
elif current == originals:
    subprocess.run([sys.executable, str(helper), str(source), '--apply'], check=True)
else:
    raise SystemExit('SVulkan2 has partial or unrelated modifications; refusing to overwrite')
PY
# Patch the Python source before wheel creation; also validate installed MPLib.
python "${BUILD_ROOT}/patch_robotwin_optional.py" --components sapien \
  --urdf-loader "${BUILD_ROOT}/SAPIEN/python/py_package/wrapper/urdf_loader.py" --apply
python "${BUILD_ROOT}/patch_robotwin_optional.py" --components mplib --apply

stage 'native SAPIEN 3.0.0b1+wsldzn build'
export CMAKE_GENERATOR=Ninja
export CMAKE_BUILD_PARALLEL_LEVEL="${SAPIEN_BUILD_JOBS}"
export MAX_JOBS="${SAPIEN_BUILD_JOBS}"
cd "${BUILD_ROOT}/SAPIEN"
python - <<'PY'
from pathlib import Path
path = Path('setup.py')
source = path.read_text()
old, new = 'version = generate_version()\n', 'version = generate_version() + "+wsldzn"\n'
if source.count(old) == 1:
    path.write_text(source.replace(old, new, 1))
elif source.count(new) != 1:
    raise SystemExit('Unknown SAPIEN setup.py version declaration')
PY
if ! python - <<'PY'
from importlib import metadata
try:
    raise SystemExit(0 if metadata.version('sapien') == '3.0.0b1+wsldzn' else 1)
except metadata.PackageNotFoundError:
    raise SystemExit(1)
PY
then
  shopt -s nullglob
  sapien_wheels=(dist/sapien-*+wsldzn-cp310-cp310-*.whl)
  if [[ ${#sapien_wheels[@]} -eq 0 ]]; then
    build_options=()
    [[ ! -f sapien_build/_sapien_install/lib/libsapien.so ]] || build_options+=(--pybind-only)
    python -u setup.py "${build_options[@]}" bdist_wheel
    sapien_wheels=(dist/sapien-*+wsldzn-cp310-cp310-*.whl)
  fi
  [[ ${#sapien_wheels[@]} -eq 1 ]] || die 'Expected one custom CPython 3.10 SAPIEN wheel.'
  python -m pip install --constraint "${REFERENCE_CONSTRAINTS}" --no-deps --force-reinstall "${sapien_wheels[0]}"
  cp "${sapien_wheels[0]}" "${WORKSPACE}/diagnostics/wheels/"
fi
python "${BUILD_ROOT}/patch_robotwin_optional.py" --components sapien --apply

stage 'CuRobo and PyTorch3D runtime dependencies under the reference lock'
# Install the entire runtime dependency closure BEFORE the existing native
# installer takes its current-environment snapshot. This preserves the complete
# reference lock even though that installer then sets its own PIP_CONSTRAINT.
python - <<'PY'
import configparser
import os
from pathlib import Path
root = Path(os.environ['BUILD_ROOT'])
parser = configparser.ConfigParser()
parser.read(root / 'curobo' / 'setup.cfg')
requirements = [x.strip() for x in parser['options']['install_requires'].splitlines() if x.strip()]
requirements += ['iopath', 'ninja', 'setuptools==69.5.1', 'setuptools-scm==8.1.0',
                 'pybind11==2.13.6', 'warp-lang==1.12.0']
(root / 'runtime-requirements.txt').write_text('\n'.join(dict.fromkeys(requirements)) + '\n')
PY
python -m pip install --constraint "${REFERENCE_CONSTRAINTS}" --requirement "${BUILD_ROOT}/runtime-requirements.txt"

stage 'sequential CUDA extension builds, with completed-build reuse'
export CUROBO_SOURCE="${BUILD_ROOT}/curobo"
export PYTORCH3D_SOURCE="${BUILD_ROOT}/pytorch3d"
export MAX_JOBS="${CUDA_BUILD_JOBS}"
export CUDA_EXTENSIONS_RUN_DIRECTORY="${WORKSPACE}/diagnostics/cuda_extensions"
cuda_state=$(python - <<'PY'
import importlib.machinery
from importlib import metadata
import json
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

try:
    dist = metadata.distribution('nvidia-curobo')
except metadata.PackageNotFoundError:
    print('fresh')
    raise SystemExit(0)
if dist.version != '0.7.8':
    raise SystemExit('An unexpected CuRobo version is installed')
direct_url = json.loads(dist.read_text('direct_url.json') or '{}')
url = urlsplit(direct_url.get('url', ''))
source = Path(os.environ['CUROBO_SOURCE']).resolve()
if (url.scheme != 'file' or url.netloc not in ('', 'localhost')
        or Path(unquote(url.path)).resolve() != source
        or direct_url.get('dir_info', {}).get('editable') is not True):
    raise SystemExit('Existing CuRobo was installed from a different source')
library = source / 'src' / 'curobo' / 'curobolib'
for name in ('lbfgs_step_cu','kinematics_fused_cu','line_search_cu','tensor_step_cu','geom_cu'):
    candidates = [library / (name + suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES]
    if not any(path.is_file() and path.stat().st_size > 0 for path in candidates):
        raise SystemExit(f'Installed CuRobo is incomplete: {name}')
try:
    p3d = metadata.distribution('pytorch3d')
except metadata.PackageNotFoundError:
    print('curobo_ready')
else:
    if p3d.version != '0.7.8':
        raise SystemExit('An unexpected PyTorch3D version is installed')
    if not any(p3d.locate_file('pytorch3d/_C' + suffix).is_file()
               for suffix in importlib.machinery.EXTENSION_SUFFIXES):
        raise SystemExit('Installed PyTorch3D native library is missing')
    print('both_ready')
PY
)
case "${cuda_state}" in
  both_ready) printf 'Both exact native builds already installed; compilation skipped\n' ;;
  curobo_ready) ROBOTWIN_SKIP_CUROBO_BUILD=1 bash "${SCRIPTS}/install_cuda_extensions.sh" ;;
  fresh) ROBOTWIN_SKIP_CUROBO_BUILD=0 bash "${SCRIPTS}/install_cuda_extensions.sh" ;;
  *) die "Unexpected CUDA build state: ${cuda_state}" ;;
esac
export PIP_CONSTRAINT="${REFERENCE_CONSTRAINTS}"

stage 'XPolicyLab shared package, without policy extras'
python -m pip install --constraint "${REFERENCE_CONSTRAINTS}" --no-build-isolation \
  --editable "${WORKSPACE}/RoboTwin/XPolicyLab"

stage 'minimal official aloha-agilex asset and absolute paths'
python "${SCRIPTS}/fetch_robot_asset.py" --cache-dir "${WORKSPACE}/cache" \
  --assets-dir "${WORKSPACE}/RoboTwin/assets" \
  --diagnostics "${WORKSPACE}/diagnostics/robot_asset.json"
cd "${WORKSPACE}/RoboTwin"
python scripts/update_embodiment_config_path.py

stage 'Python dependency consistency'
python -m pip check

stage 'CUDA, CPU physics, raster rendering and one local expert demonstration'
cd "${WORKSPACE}"
python "${SCRIPTS}/check_base.py"
python "${SCRIPTS}/check_cuda_extensions.py"
python "${SCRIPTS}/check_sapien.py" physics
python "${SCRIPTS}/check_sapien.py" render
python "${SCRIPTS}/check_robotwin_task.py" --robotwin-root "${WORKSPACE}/RoboTwin" \
  --output-dir "${WORKSPACE}/diagnostics/robotwin_task" --seed 0 --play-once
python - <<'PY'
from datetime import datetime, timezone
from importlib import metadata
import json
from pathlib import Path
report = {'passed': True, 'completed_utc': datetime.now(timezone.utc).isoformat(),
          'task': 'stack_blocks_two', 'seed': 0, 'expert_play_once': True,
          'versions': {name: metadata.version(name) for name in (
              'torch','torchvision','triton','numpy','scipy','sapien','nvidia-curobo',
              'pytorch3d','warp-lang','xpolicylab')}}
Path('/workspace/diagnostics/bootstrap-complete.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
PY
printf '\nReproduction setup and all local validation checks completed.\n'
