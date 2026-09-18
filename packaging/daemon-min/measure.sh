#!/usr/bin/env bash
# 复现 docs/spikes/02-packaging.md §1 表格里的冷启动 / 空闲 RSS 数字。
#
# 冷启动计时方式（第二轮评审后修正）：measure.sh 自己不再反复 spawn 探测子进程去猜
# "socket 什么时候能连上"——那样会把探测子进程自身的启动开销（每次 fork+exec 一个
# python3 解释器）计进"冷启动"数字里，两条打包路线的差值就失去意义。现在改为：
# ping_daemon.py 在 socket 进入 accept 状态的那一刻，把自己的单调时钟时间戳
# （time.monotonic_ns()）写进结构化日志（见该文件 "started" 事件的 ready_monotonic_ns
# 字段）；本脚本只负责在 spawn 前后各取一次自己的 monotonic_ns，用 daemon 报告的
# ready 时间戳减去 spawn 前的时间戳，得到 spawn → ready 之间的真实耗时。
# CLOCK_MONOTONIC 在同一台机器上跨进程是可比的（不是各进程独立计时），所以两边分别
# 取时间戳、事后相减是成立的。
#
# 不用 `date +%s%N` 取纳秒时间戳：macOS 自带的 BSD date 不支持 %N（会原样输出字面量
# "N"，不是纳秒数），只有 GNU date 支持。改用 python3 的 time.monotonic_ns()，
# 跨平台且和 daemon 自己用的是同一个时钟源。
#
# 用法：./measure.sh <daemon 可执行文件或启动脚本> [重复次数，默认 3]
#   ./measure.sh ../standalone/dist/arm64/run.sh
#   ./measure.sh ../pyinstaller/dist/jones-daemon-spike/jones-daemon-spike
set -euo pipefail

EXE="${1:?usage: measure.sh <path-to-daemon-executable> [runs]}"
RUNS="${2:-3}"

# 轮询等待 daemon 写出 ready 日志行的超时参数。轮询本身只用来判断"该不该继续等"，
# 不参与冷启动耗时的计算（真正的耗时来自 daemon 自己报告的 ready_monotonic_ns），
# 所以轮询间隔用 shell 内置的 sleep + grep，不再每次都拉起一个 python3 子进程。
POLL_INTERVAL_S=0.02
POLL_MAX_TRIES=150
POLL_TIMEOUT_S=$(python3 -c "print(f'{$POLL_INTERVAL_S * $POLL_MAX_TRIES:.1f}')")

mono_ns() {
  python3 -c 'import time; print(time.monotonic_ns())'
}

median() {
  # 读 stdin 里的整数（每行一个），打印中位数（偶数个时取中间两个的平均，向下取整）。
  python3 -c '
import sys
vals = sorted(int(x) for x in sys.stdin.read().split())
n = len(vals)
print(vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) // 2)
'
}

HOME_DIR=$(mktemp -d)
SOCK="$HOME_DIR/daemon.sock"
LOG="$HOME_DIR/daemon.log"
trap 'rm -rf "$HOME_DIR"' EXIT

boot_values=()
rss_values=()

for i in $(seq 1 "$RUNS"); do
  rm -f "$SOCK" "$LOG"
  start_ns=$(mono_ns)
  JONES_SPIKE_HOME="$HOME_DIR" "$EXE" &
  pid=$!

  ready_ns=""
  for _ in $(seq 1 "$POLL_MAX_TRIES"); do
    if [ -f "$LOG" ]; then
      line=$(grep -m1 '"event": "started"' "$LOG" 2>/dev/null || true)
      if [ -n "$line" ]; then
        ready_ns=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['ready_monotonic_ns'])" "$line")
        break
      fi
    fi
    sleep "$POLL_INTERVAL_S"
  done

  if [ -z "$ready_ns" ]; then
    echo "run $i: daemon did not report ready within ${POLL_TIMEOUT_S}s (see $LOG)" >&2
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    exit 1
  fi

  boot_ms=$(( (ready_ns - start_ns) / 1000000 ))
  sleep 0.2   # 给 RSS 一点时间稳定到空闲态，再读
  rss_kb=$(ps -o rss= -p "$pid" 2>/dev/null | tr -d ' ')
  if [ -z "$rss_kb" ]; then
    echo "run $i: process $pid exited before RSS could be read (see $LOG)" >&2
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    exit 1
  fi

  echo "run $i: boot_ms=$boot_ms idle_rss_kb=$rss_kb"
  boot_values+=("$boot_ms")
  rss_values+=("$rss_kb")

  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
done

boot_median=$(printf '%s\n' "${boot_values[@]}" | median)
rss_median=$(printf '%s\n' "${rss_values[@]}" | median)

echo "---"
echo "median boot_ms=$boot_median median idle_rss_kb=$rss_median over $RUNS runs"
