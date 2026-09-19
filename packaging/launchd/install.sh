#!/usr/bin/env bash
# 草案：把 ai.jones.daemon.plist 安装为用户级 LaunchAgent（第二轮评审新增，见修复记录）。
# 跟 plist 本身一样是草案——真实实现是 `python -m jones_daemon service install`
# （daemon/src/jones_daemon/service.py，Issue #6：用 plistlib 直接生成、launchctl
# bootstrap/bootout 可注入 runner、有单测），这里只保留作 spike 阶段的可读参考实现，
# 不再是安装路径的最终位置。
#
# 供本 spike 验证"安装脚本本身"这一步的正确性，不代表真实实现的最终位置。
#
# 为什么需要预创建日志目录：launchd 不会帮 StandardOutPath / StandardErrorPath 自动创建
# 父目录——如果 ~/.jones/runtime/logs/ 不存在，job 要么启动失败（bootstrap 报错），要么
# 静默丢失 stdout/stderr（行为因 macOS 版本而异，不可依赖），两种结果都会让"崩溃后看不到
# daemon 自己的日志"这个本该由 launchd 兜底的能力失效。所以必须在 bootstrap 之前先建好目录。
#
# 用法：./install.sh <daemon 可执行文件绝对路径> [JONES_HOME，默认 ~/.jones]
set -euo pipefail
cd "$(dirname "$0")"

DAEMON_EXECUTABLE="${1:?usage: install.sh <path-to-daemon-executable> [jones-home]}"
JONES_HOME="${2:-$HOME/.jones}"

if [ ! -x "$DAEMON_EXECUTABLE" ]; then
  echo "error: daemon executable not found or not executable: $DAEMON_EXECUTABLE" >&2
  exit 1
fi

LOG_DIR="$JONES_HOME/runtime/logs"
mkdir -p "$LOG_DIR"
echo "log dir ready: $LOG_DIR"

LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$LAUNCH_AGENTS_DIR"
DEST_PLIST="$LAUNCH_AGENTS_DIR/ai.jones.daemon.plist"

sed \
  -e "s#__DAEMON_EXECUTABLE__#${DAEMON_EXECUTABLE}#g" \
  -e "s#__JONES_HOME__#${JONES_HOME}#g" \
  ai.jones.daemon.plist > "$DEST_PLIST"
echo "wrote: $DEST_PLIST"

# 本脚本写到这里为止：不在本 spike 里实际执行 launchctl bootstrap（会在本机常驻注册一个
# 真实服务，超出 spike 范围，见 docs/spikes/02-packaging.md「没做什么」与原报告）。
# 真实安装流程接下来要跑：
echo "next step (not run by this script): launchctl bootstrap gui/\$(id -u) \"$DEST_PLIST\""
