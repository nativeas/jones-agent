#!/usr/bin/env bash
# Black-box smoke test against a REAL packaged .app (docs/design/
# 05-w6-interfaces.md §3.2 "smoke 在打包产物上再跑一次"). Unlike
# apps/desktop/scripts/smoke.mjs / e2e.mjs (which run in-process against the
# built `out/main/index.js`, `app.isPackaged === false`), this launches the
# actual `.app` binary as a separate OS process — the only way to exercise
# `app.isPackaged === true`, which is exactly the code path this branch
# added (`findPackagedDaemonExecutable()` / the packaged-mode branch of
# `spawnDevDaemon()` in apps/desktop/src/main/index.ts). If that path were
# broken, this is the test that would catch it; the in-process e2e never
# sets `app.isPackaged`.
#
# Usage: smoke-packaged-app.sh <path-to-.app>
set -euo pipefail
APP="${1:?usage: smoke-packaged-app.sh <path-to-.app>}"
PRODUCT_NAME="$(basename "$APP" .app)"
BINARY="$APP/Contents/MacOS/$PRODUCT_NAME"
if [ ! -x "$BINARY" ]; then
  echo "FAIL: no executable at $BINARY" >&2
  exit 1
fi

JONES_HOME="$(mktemp -d /tmp/jones-packaged-smoke.XXXXXX)"
SOCK="$JONES_HOME/runtime/daemon.sock"
PID_FILE="$JONES_HOME/runtime/daemon.pid"

cleanup() {
  # The daemon is spawned `detached: true` (by design — it outlives Electron,
  # 00-foundation.md §3), so killing the app process below does NOT also
  # kill it; clean it up separately via its own documented PID file, same
  # pattern apps/desktop/scripts/e2e.mjs uses.
  if [ -f "$PID_FILE" ]; then
    kill -TERM "$(cat "$PID_FILE")" 2>/dev/null || true
  fi
  [ -n "${APP_PID:-}" ] && kill -TERM "$APP_PID" 2>/dev/null || true
  sleep 0.5
  rm -rf "$JONES_HOME"
}
trap cleanup EXIT

echo "launching $BINARY (JONES_HOME=$JONES_HOME)"
JONES_HOME="$JONES_HOME" "$BINARY" >/tmp/jones-packaged-smoke.log 2>&1 &
APP_PID=$!

DEADLINE=$(( $(date +%s) + 20 ))
CONNECTED=0
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  if [ -S "$SOCK" ]; then
    if python3 -c "
import socket, json, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(2)
s.connect('$SOCK')
s.sendall((json.dumps({'jsonrpc':'2.0','id':'1','method':'daemon.ping'})+'\n').encode())
resp = json.loads(s.recv(65536).decode().splitlines()[0])
s.close()
sys.exit(0 if 'result' in resp else 1)
" 2>/dev/null; then
      CONNECTED=1
      break
    fi
  fi
  if ! kill -0 "$APP_PID" 2>/dev/null; then
    echo "FAIL: app process exited early — log:" >&2
    cat /tmp/jones-packaged-smoke.log >&2
    exit 1
  fi
  sleep 0.3
done

if [ "$CONNECTED" -ne 1 ]; then
  echo "FAIL: daemon never became reachable at $SOCK within 20s (packaged spawnDevDaemon() path never got the daemon up) — app log:" >&2
  cat /tmp/jones-packaged-smoke.log >&2
  exit 1
fi

echo "OK: packaged .app launched, ensureDaemonRunning()'s packaged-mode spawn (findPackagedDaemonExecutable()) brought up a real daemon.ping-reachable daemon"
