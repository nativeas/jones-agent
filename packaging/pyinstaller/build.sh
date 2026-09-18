#!/usr/bin/env bash
# 用 PyInstaller 把 packaging/daemon-min/ping_daemon.py 打成 onedir 可执行目录。
# `uv venv --python 3.12` 解析到的是 thin（单架构）基础解释器，所以这里只能打出与本机
# 相同架构的产物——不是 PyInstaller 在 macOS 上做不到跨架构：给它一个 universal2 基础
# 解释器（如 python.org 官方安装器装出来的那种），PyInstaller 能正确产出目标架构的产物
# （已实测验证，见 docs/spikes/02-packaging.md §1「根因更正」）。本仓库解释器工具链 uv
# 不提供 universal2 build，只有 per-arch thin build，这也是最终选 python-build-standalone
# 而不是这条路线的真实原因。
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
