#!/usr/bin/env bash
# 复现 docs/spikes/02-packaging.md §1 表格里的冷启动 / 空闲 RSS 数字。
# 外部计时：从 spawn 到 socket 可连接为止，daemon 进程内部无法测出自己的冷启动
# 耗时（exec 之前的时间它根本看不到），所以计时必须由调用方做，这也是
# ping_daemon.py 不再自己记 boot_s 的原因（见该文件与评审记录）。
#
# 用法：./measure.sh <daemon 可执行文件或启动脚本> [重复次数，默认 5]
#   ./measure.sh ../standalone/dist/arm64/run.sh
#   ./measure.sh ../pyinstaller/dist/jones-daemon-spike/jones-daemon-spike
set -euo pipefail

EXE="${1:?usage: measure.sh <path-to-daemon-executable> [runs]}"
RUNS="${2:-5}"

HOME_DIR=$(mktemp -d)
SOCK="$HOME_DIR/daemon.sock"
trap 'rm -rf "$HOME_DIR"' EXIT

boot_total=0
rss_total=0

for i in $(seq 1 "$RUNS"); do
  rm -f "$SOCK"
  start_ns=$(date +%s%N)
  JONES_SPIKE_HOME="$HOME_DIR" "$EXE" &
  pid=$!

  ok=0
  for _ in $(seq 1 400); do
    if [ -S "$SOCK" ] && python3 -c "
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    s.connect('$SOCK')
except OSError:
    sys.exit(1)
" 2>/dev/null; then
      ok=1
      break
    fi
    sleep 0.005
  done
  end_ns=$(date +%s%N)

  if [ "$ok" -ne 1 ]; then
    echo "run $i: socket never became connectable within 2s" >&2
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    exit 1
  fi

  boot_ms=$(( (end_ns - start_ns) / 1000000 ))
  sleep 0.2   # 给 RSS 一点时间稳定到空闲态，再读
  rss_kb=$(ps -o rss= -p "$pid" | tr -d ' ')
  echo "run $i: boot_ms=$boot_ms idle_rss_kb=$rss_kb"
  boot_total=$((boot_total + boot_ms))
  rss_total=$((rss_total + rss_kb))

  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
done

echo "---"
echo "avg boot_ms=$((boot_total / RUNS)) avg idle_rss_kb=$((rss_total / RUNS)) over $RUNS runs"
