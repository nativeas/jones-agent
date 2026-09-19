#!/usr/bin/env bash
# Produces a signed (ad-hoc) .dmg for macOS (docs/design/05-w6-interfaces.md
# §3.2, Issue #25): python-build-standalone daemon bundle(s) (interpreter +
# real site-packages + Hermes source tree, see build-daemon-bundle.sh) +
# electron-builder packaging of apps/desktop, extraResources-ing the daemon
# bundle in, then an afterPack ad-hoc signing pass (packaging/release/afterPack.cjs).
#
# Usage: scripts/release/build-mac.sh [arm64|x64|both]   (default: both)
#
# arm64 is this repo's native architecture — the produced .app/.dmg actually
# RUNS here, and this script's own smoke/e2e step (below, run manually — see
# docs/release.md) exercises it for real.
#
# x64 CURRENTLY FAILS TO BUILD on this host, not just "can't be run here":
# `cryptography==50.0.0` (daemon's exact-pinned dependency) has no macOS
# x86_64 wheel on PyPI for this version, so `uv pip install --python-platform
# x86_64-apple-darwin` falls back to a from-source build that can't
# cross-compile on an arm64 host — see build-daemon-bundle.sh's comment on
# that step and docs/release.md §2.2 for the full real-vs-spike finding
# (spike 02's "x86_64 only needs Rosetta to run, not to build" conclusion
# held only for its dependency-free daemon skeleton, not for real Hermes
# dependencies). Real x64 verification needs a native Intel/CI runner.
set -euo pipefail

TARGET="${1:-both}"
case "$TARGET" in
  arm64|x64) ARCHES=("$TARGET") ;;
  both) ARCHES=(arm64 x64) ;;
  *) echo "usage: build-mac.sh [arm64|x64|both]" >&2; exit 1 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DESKTOP_DIR="$REPO_ROOT/apps/desktop"
RELEASE_PKG_DIR="$REPO_ROOT/packaging/release"
BUILD_DIR="$REPO_ROOT/build/release"

echo "== 1/4: daemon bundle(s) =="
for arch in "${ARCHES[@]}"; do
  "$SCRIPT_DIR/build-daemon-bundle.sh" "$arch" "$BUILD_DIR/daemon/$arch"
done

echo "== 2/4: apps/desktop build (electron-vite) =="
( cd "$DESKTOP_DIR" && pnpm install --frozen-lockfile && pnpm run build )

echo "== 3/4: electron-builder toolchain =="
( cd "$RELEASE_PKG_DIR" && pnpm install --frozen-lockfile )
RESOLVED_CONFIG="$BUILD_DIR/electron-builder.resolved.yml"
mkdir -p "$BUILD_DIR"
sed "s#__REPO_ROOT__#$REPO_ROOT#g" "$RELEASE_PKG_DIR/electron-builder.yml" > "$RESOLVED_CONFIG"

echo "== 4/4: package + ad-hoc sign =="
for arch in "${ARCHES[@]}"; do
  echo "--- $arch ---"
  ARCH_FLAG="--$arch"
  ( cd "$RELEASE_PKG_DIR" && \
    ./node_modules/.bin/electron-builder \
      --project "$DESKTOP_DIR" \
      --config "$RESOLVED_CONFIG" \
      --mac dmg "$ARCH_FLAG" )
done

echo
echo "done. artifacts under: $BUILD_DIR/dist"
ls -la "$BUILD_DIR/dist" 2>/dev/null || true
