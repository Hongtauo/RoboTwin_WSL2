#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  git ca-certificates vulkan-tools libvulkan1 libgl1 libglib2.0-0 \
  libx11-6 libx11-xcb1 libxcb1 libxrandr2 libxinerama1 libxcursor1 libxi6
python -m pip install --disable-pip-version-check 'numpy==1.26.4' 'sapien==3.0.0b1' pillow
