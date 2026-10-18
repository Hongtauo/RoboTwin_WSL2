#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get install -y --no-install-recommends build-essential pkg-config \
  libx11-dev libxrandr-dev libxinerama-dev libxcursor-dev libxi-dev libxext-dev libgl-dev
/opt/conda/envs/robotwin/bin/python -m pip install --disable-pip-version-check \
  'setuptools==69.5.1' wheel 'numpy==1.26.4' pillow requests \
  'transforms3d==0.4.2' lxml networkx pyperclip 'opencv-python==4.10.0.84'
