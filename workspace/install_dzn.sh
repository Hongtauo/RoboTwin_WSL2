#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get install -y --no-install-recommends gpg
python - <<'PY'
from pathlib import Path
from urllib.request import urlopen
url = 'https://keyserver.ubuntu.com/pks/lookup?op=get&search=0xEB8B81E14DA65431D7504EA8F63F0F2B90935439'
with urlopen(url, timeout=40) as response:
    key = response.read(1024 * 1024)
Path('/usr/share/keyrings/kisak-turtle.asc').write_bytes(key)
PY
fingerprint=$(gpg --batch --show-keys --with-colons /usr/share/keyrings/kisak-turtle.asc | awk -F: '$1 == "fpr" {print $10; exit}')
test "$fingerprint" = EB8B81E14DA65431D7504EA8F63F0F2B90935439
printf '%s\n' 'deb [arch=amd64 signed-by=/usr/share/keyrings/kisak-turtle.asc] https://ppa.launchpadcontent.net/kisak/turtle/ubuntu jammy main' > /etc/apt/sources.list.d/kisak-turtle.list
apt-get update
apt-get install -y --no-install-recommends 'mesa-vulkan-drivers=25.0.7~kisak3~j'
test -f /usr/share/vulkan/icd.d/dzn_icd.x86_64.json
test -f /usr/lib/x86_64-linux-gnu/libvulkan_dzn.so
dpkg-query -W mesa-vulkan-drivers libdrm2
