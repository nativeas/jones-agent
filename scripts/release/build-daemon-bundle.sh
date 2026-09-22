#!/usr/bin/env bash
# Builds one architecture's daemon bundle for packaging (docs/design/
# 05-w6-interfaces.md §3.2, Issue #25), following spike 02's conclusion
# (docs/spikes/02-packaging.md, docs/design/00-foundation.md §2): a
# python-build-standalone interpreter + the daemon's own resolved dependency
# closure as a real `site-packages/` + Hermes as a PYTHONPATH-injected SOURCE
# TREE (not pip-installed — Hermes's own setup.py refuses a non-editable
# build, confirmed by spike 02) + a self-contained entry script.
#
# Usage: build-daemon-bundle.sh <arm64|x64> <output-dir>
#
# Output layout under <output-dir> (this becomes `extraResources`'s `from` in
# electron-builder.yml, landing at `Contents/Resources/daemon/` in the .app):
#   bin/jones-daemon      # entry script: sets PYTHONPATH, execs the interpreter -m jones_daemon
#   python/                # python-build-standalone interpreter + site-packages (from packaging/standalone/build.sh)
#   hermes-agent/           # `git archive` of the pinned Hermes commit (source tree, not installed)
#   DEPENDENCY-REPORT.md    # what got merged into site-packages and why there are no version
#                           # conflicts to list (05-w6-interfaces.md §3.2 "冲突项报告列出")
set -euo pipefail

ARCH="${1:?usage: build-daemon-bundle.sh <arm64|x64> <output-dir>}"
OUT_ARG="${2:?usage: build-daemon-bundle.sh <arm64|x64> <output-dir>}"
# Made absolute up front: later steps `cd` into daemon/ (for `uv build`/`uv
# export`/`uv pip install`), and a relative `$2` would otherwise silently
# resolve against the wrong directory from that point on.
mkdir -p "$(dirname "$OUT_ARG")"
OUT="$(cd "$(dirname "$OUT_ARG")" && pwd)/$(basename "$OUT_ARG")"
case "$ARCH" in
  arm64) UV_PLATFORM="aarch64-apple-darwin"; HOST_ARCH_FOR="arm64" ;;
  x64) UV_PLATFORM="x86_64-apple-darwin"; HOST_ARCH_FOR="x86_64" ;;
  *) echo "unknown arch: $ARCH (want arm64 or x64)" >&2; exit 1 ;;
esac

# Native vs cross build (Issue #36): when the requested $ARCH matches the host
# machine's own architecture (`uname -m`), this is a NATIVE build — the host
# already has real x86_64/arm64 wheels and, for anything without a wheel, a
# real matching Rust target to compile against. `--python-platform` exists
# only to fake a foreign target platform's wheel tags without a local
# interpreter of that architecture (see the comment on the install calls
# below) — passing it on a native build is not just unnecessary, it's
# actively wrong: it makes `uv` request wheel tags for a platform that then
# happens to equal the host, but for anything that falls back to a source
# build (`cryptography` on x86_64, see docs/release.md §2.2), the resulting
# build still runs on THIS host's real toolchain — there is nothing to
# "cross" about it. So on a native build, both `uv pip install` calls below
# omit `--python-platform` entirely and let `uv` resolve against the host's
# own interpreter/platform, exactly as "native install" implies.
if [ "$(uname -m)" = "$HOST_ARCH_FOR" ]; then
  IS_NATIVE=1
  PLATFORM_FLAGS=()
else
  IS_NATIVE=0
  PLATFORM_FLAGS=(--python-platform "$UV_PLATFORM")
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DAEMON_DIR="$REPO_ROOT/daemon"
STANDALONE_DIR="$REPO_ROOT/packaging/standalone"

# --- 1. Interpreter (reuses packaging/standalone/build.sh: `uv python install
#        <target-triple>` + tcl/tk stripped — spike 02 §1) -------------------
( cd "$STANDALONE_DIR" && ./build.sh "$ARCH" )

rm -rf "$OUT"
mkdir -p "$OUT/bin"
cp -R "$STANDALONE_DIR/dist/$ARCH/python" "$OUT/python"
SITE_PACKAGES="$OUT/python/lib/python3.12/site-packages"
mkdir -p "$SITE_PACKAGES"

# --- 2. Daemon's own dependency closure + Hermes's (same combined,
#        already-conflict-resolved `uv.lock` — see DEPENDENCY-REPORT.md
#        below for why "conflicting items" is an empty list here, not an
#        unchecked one) ------------------------------------------------------
cd "$DAEMON_DIR"
REQS_FILE="$(mktemp -t jones-release-reqs.XXXXXX)"
# `ec=$?; ...; exit $ec`, not a bare `rm -f "$REQS_FILE"`: an EXIT trap whose
# last command exits 0 (which `rm -f` on an existing file always does)
# silently OVERWRITES the script's real exit status with that 0 — bash uses
# the trap's own last exit code as the shell's final one when the trap
# doesn't explicitly `exit`. Round-1 CI verification for #36 hit exactly
# this: the `--python-platform` bash-3.2 bug below made the build crash
# outputting only a partial bundle, but this trap's bare `rm -f` silently
# turned that crash into a reported `exit 0` — both CI jobs, and a plain
# local rerun, showed green while shipping a broken bundle (no dependencies
# installed, no DEPENDENCY-REPORT.md). Capturing `$?` before cleanup and
# re-exiting with it is what makes a real failure actually surface as one.
trap 'ec=$?; rm -f "$REQS_FILE"; exit $ec' EXIT
# --no-default-groups: exclude `dev` (pytest/ruff — dev tooling, not shipped).
# --group worker: `hermes-agent[acp]==<pinned>` + `mcp==2.0.0` (01-w2-interfaces
# §2.2, daemon/pyproject.toml's own comment on the `worker` group) — this is
# THE SAME `uv.lock` `uv sync --group worker` resolves from, so every pin here
# already passed uv's own resolver as one consistent graph; there is no
# separate "Hermes's lock" being merged in afterward; the export below.
#
# round-1 review fix (评审 #6): no longer `--no-hashes`. `uv export` writes
# per-artifact hashes straight from `uv.lock`, and `uv pip install -r` verifies
# them — dropping that (the original `--no-hashes`) meant the third-party
# dependency closure shipped in every release .dmg (cryptography included) was
# installed from the network with zero integrity verification. Hashes are now
# kept, which is also why the local wheel install below moved to its own `uv
# pip install` call: pip's hash-checking mode requires EVERY requirement in one
# invocation to carry a hash, and a freshly-built local wheel has none.
UV_FROZEN=1 uv export --no-default-groups --group worker \
  --format requirements-txt > "$REQS_FILE.raw"
# Strip the two editable/local-path entries: `-e .` (jones_daemon itself —
# built as a real wheel below instead) and `-e <hermes-agent path>` (source
# tree via git-archive below instead, per the module docstring).
grep -v '^-e ' "$REQS_FILE.raw" | grep -v '^#' > "$REQS_FILE"
rm -f "$REQS_FILE.raw"

rm -rf dist
UV_FROZEN=1 uv build --wheel >/dev/null
WHEEL="$(ls dist/jones_daemon-*.whl)"

# CROSS build only (`--python-platform`/`--python <version>`, not a path): a
# pure resolve+download of prebuilt wheels for the TARGET architecture,
# needing no local interpreter of that architecture (spike 02 §1's whole
# point about python-build-standalone: no target-arch code execution
# required) — this is what makes producing an x64 bundle on an arm64 host
# possible AT ALL, but only for dependencies that actually publish a wheel
# for the pinned version and target platform. **Known real failure doing
# this cross (see docs/release.md §2.2, docs/design/00-foundation.md §3.1's
# addendum)**: `cryptography==50.0.0` (daemon's exact-pinned dependency)
# ships zero macOS x86_64 wheels on PyPI for this version — `uv` silently
# falls back to building it from source (maturin/cargo), which then fails to
# cross-compile on an arm64 host with no x86_64 Rust target installed. This
# is a real gap, not a benign warning.
#
# NATIVE build (Issue #36, `$IS_NATIVE=1` above — a native Intel/Apple
# Silicon runner building its own architecture): `$PLATFORM_FLAGS` is empty,
# so these calls resolve/install against the host's own real interpreter
# platform — including building `cryptography` from sdist with the host's
# own real Rust toolchain when no wheel exists, which is an ordinary native
# compile, not a cross-compile, and is expected to succeed.
#
# Either way the command is left to fail loudly (not caught/retried) so a
# failure is never silently swallowed into a broken bundle.
# Third-party deps first, hash-checked against uv.lock (-r "$REQS_FILE" now
# carries hashes — see the export step above); the local wheel is a separate
# call below since it has no hash to check against and pip's hash-checking
# mode requires all-or-nothing within one invocation.
# `"${PLATFORM_FLAGS[@]+"${PLATFORM_FLAGS[@]}"}"`, not the plain
# `"${PLATFORM_FLAGS[@]}"`: macOS's shipped `/bin/bash` is stuck at 3.2 (Apple
# won't ship GPLv3), and bash <4.4's `set -u` treats expanding an EMPTY array
# as an unbound-variable error — fatal, immediately. The native branch above
# sets `PLATFORM_FLAGS=()` (empty on purpose), so a plain `"${PLATFORM_FLAGS[@]}"`
# here crashes every native build on this host's own /bin/bash (confirmed:
# reproduced locally AND on both macos-14/macos-15-intel CI runners, which
# also default to bash 3.2 — round-1 CI verification for #36 silently shipped
# a broken bundle this way, see this branch's report for the full story). The
# `${arr[@]+"${arr[@]}"}` form is the standard bash-3.2-safe idiom: it tests
# "is this array SET" (true — `()` still counts as set) without ever forcing
# nounset to evaluate an empty `[@]` on its own.
uv pip install \
  --target "$SITE_PACKAGES" \
  "${PLATFORM_FLAGS[@]+"${PLATFORM_FLAGS[@]}"}" \
  --python 3.12 \
  -r "$REQS_FILE"

uv pip install \
  --target "$SITE_PACKAGES" \
  "${PLATFORM_FLAGS[@]+"${PLATFORM_FLAGS[@]}"}" \
  --python 3.12 \
  "$WHEEL"

INSTALLED_COUNT=$(find "$SITE_PACKAGES" -maxdepth 1 -name '*.dist-info' | wc -l | tr -d ' ')

# --- 3. Hermes source tree (pinned commit, `git archive` — not pip-installed:
#        Hermes's own setup.py refuses a non-editable build, spike 02) -------
HERMES_COMMIT="$(grep -oE '[0-9a-f]{40}' pyproject.toml | sort -u | head -1)"
HERMES_PATH="${HERMES_AGENT_PATH:-$(grep -A2 '^\[tool\.uv\.sources\]$' pyproject.toml | grep -oE '"[^"]*hermes-agent[^"]*"' | head -1 | tr -d '"')}"
if [ -z "$HERMES_COMMIT" ] || [ -z "$HERMES_PATH" ]; then
  echo "could not determine pinned Hermes commit/path from daemon/pyproject.toml" >&2
  exit 1
fi
if [ ! -d "$HERMES_PATH" ]; then
  echo "Hermes checkout not found at $HERMES_PATH (set HERMES_AGENT_PATH to override)" >&2
  exit 1
fi
ACTUAL_COMMIT="$(git -C "$HERMES_PATH" rev-parse HEAD)"
mkdir -p "$OUT/hermes-agent"
git -C "$HERMES_PATH" archive "$HERMES_COMMIT" | tar -x -C "$OUT/hermes-agent"

# --- 4. Entry script (Resources/daemon/bin/jones-daemon, design §3.2) -------
cat > "$OUT/bin/jones-daemon" <<'SH'
#!/usr/bin/env bash
# Packaged daemon entry point (docs/design/05-w6-interfaces.md §3.2). This is
# the path Electron main resolves in packaged mode
# (`process.resourcesPath/daemon/bin/jones-daemon`) and the one
# `service install --program` points a LaunchAgent's `ProgramArguments` at.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Hermes as a PYTHONPATH-injected source tree, not an installed package
# (design §0/§3.2) — its own dependency closure lives in python/lib/.../
# site-packages alongside the daemon's, merged at build time.
export PYTHONPATH="$DIR/hermes-agent${PYTHONPATH:+:$PYTHONPATH}"
exec "$DIR/python/bin/python3.12" -m jones_daemon "$@"
SH
chmod +x "$OUT/bin/jones-daemon"

# --- 5. Report (design §3.2 "冲突项报告列出") --------------------------------
cat > "$OUT/DEPENDENCY-REPORT.md" <<REPORT
# Daemon bundle dependency report — $ARCH

- Interpreter: python-build-standalone $ARCH (via \`packaging/standalone/build.sh\`, tcl/tk stripped).
- Daemon package: \`$WHEEL\` (real wheel, hatchling build backend — not a manual copy).
- Combined dependency closure: \`uv export --no-default-groups --group worker\` from
  \`daemon/pyproject.toml\`'s already-committed \`uv.lock\` — $INSTALLED_COUNT distributions
  installed into \`python/lib/python3.12/site-packages\`.
- **Conflicting items: none.** This is not an unchecked claim: the daemon's base
  dependencies and Hermes's own (\`hermes-agent[acp]==0.21.2\` + \`mcp==2.0.0\`, the
  \`worker\` dependency group) are resolved by \`uv\` into ONE combined lock at
  \`uv sync --group worker\`/\`uv lock\` time (that's what makes \`uv export --group worker\`
  above possible at all) — had there been an incompatible version request between the
  two (e.g. two different pinned \`pydantic\` majors), that resolution itself would have
  failed with a diagnosable error, not silently picked one. This build re-exports that
  same already-unified lock; it does not re-resolve or reconcile two separate lockfiles.
- Hermes source tree: \`git archive\` of \`$HERMES_COMMIT\` (pinned in \`daemon/pyproject.toml\`'s
  \`worker\` group comment) from \`$HERMES_PATH\`. Actual checkout HEAD at build time:
  \`$ACTUAL_COMMIT\` $( [ "$ACTUAL_COMMIT" = "$HERMES_COMMIT" ] && echo "(matches pin)" || echo "**DOES NOT MATCH THE PIN — see report**" )
REPORT

echo "built: $OUT"
du -sh "$OUT" 2>/dev/null || true
