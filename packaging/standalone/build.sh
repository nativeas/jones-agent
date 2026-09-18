#!/usr/bin/env bash
# 方案 B：python-build-standalone（uv 自带的同一份分发）+ 直接拷贝，不冻结/不 freeze。
# 与 PyInstaller 的关键区别：这里只是"下载解释器 tarball + 拷贝脚本"，不需要在目标架构上
# 执行任何代码，所以可以在 Apple Silicon 主机上同时产出 arm64 与 x86_64 两份产物
# （PyInstaller 做不到：它要 exec 目标解释器来 freeze，因此只能产出与当前主机同架构的包）。
#
# 用法：./build.sh arm64|x64
set -euo pipefail
cd "$(dirname "$0")"

ARCH="${1:?usage: build.sh arm64|x64}"
case "$ARCH" in
  arm64) TARGET="cpython-3.12-macos-aarch64-none" ;;
  x64)   TARGET="cpython-3.12-macos-x86_64-none" ;;
  *) echo "unknown arch: $ARCH" >&2; exit 1 ;;
esac

mkdir -p pyroot
uv python install "$TARGET" --install-dir ./pyroot

SRC_DIR=$(find pyroot -maxdepth 1 -type d -name "cpython-3.12.*-${TARGET#cpython-3.12-}" | head -1)
if [ -z "$SRC_DIR" ]; then
  echo "could not find installed interpreter dir for $TARGET under pyroot/" >&2
  exit 1
fi

OUT="dist/$ARCH"
rm -rf "$OUT"
mkdir -p "$OUT"
# 整个解释器目录（含 lib/pythonX.Y、动态库）原样拷贝——这是唯一必需的产物，
# 不需要 pip / venv，因为我们的 spike 脚本是纯标准库。真实 daemon 若有第三方依赖
# （如 hermes-agent），需要额外把 site-packages 拷进 lib/python3.12/site-packages。
cp -R "$SRC_DIR" "$OUT/python"
mkdir -p "$OUT/app"
cp ../daemon-min/ping_daemon.py "$OUT/app/ping_daemon.py"

cat > "$OUT/run.sh" <<'SH'
#!/usr/bin/env bash
# 启动脚本：真实打包时这个逻辑会被 launchd plist 的 ProgramArguments 直接调用可执行文件，
# 这里用 shell 包一层只是为了本地测试方便解析相对路径。
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/python/bin/python3.12" "$DIR/app/ping_daemon.py"
SH
chmod +x "$OUT/run.sh"

echo "built: $(pwd)/$OUT"
du -sh "$OUT"
