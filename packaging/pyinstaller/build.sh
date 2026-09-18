#!/usr/bin/env bash
# 用 PyInstaller 把 packaging/daemon-min/ping_daemon.py 打成 onedir 可执行目录。
# 只能打出与本机相同架构的产物（PyInstaller 冻结时要 exec 目标解释器，无法跨架构）。
set -euo pipefail
cd "$(dirname "$0")"

VENV=.venv
uv venv "$VENV" --python 3.12 -q
# shellcheck disable=SC1091
source "$VENV/bin/activate"
uv pip install -q pyinstaller==6.11.1

rm -rf build dist ping_daemon.spec
pyinstaller \
  --name jones-daemon-spike \
  --onedir \
  --noupx \
  --clean \
  --distpath dist \
  --workpath build \
  ../daemon-min/ping_daemon.py

echo "built: $(pwd)/dist/jones-daemon-spike"
du -sh dist/jones-daemon-spike
