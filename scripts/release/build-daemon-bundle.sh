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
  arm64) UV_PLATFORM="aarch64-apple-darwin" ;;
  x64) UV_PLATFORM="x86_64-apple-darwin" ;;
  *) echo "unknown arch: $ARCH (want arm64 or x64)" >&2; exit 1 ;;
esac

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
trap 'rm -f "$REQS_FILE"' EXIT
# --no-default-groups: exclude `dev` (pytest/ruff — dev tooling, not shipped).
# --group worker: `hermes-agent[acp]==<pinned>` + `mcp==2.0.0` (01-w2-interfaces
# §2.2, daemon/pyproject.toml's own comment on the `worker` group) — this is
# THE SAME `uv.lock` `uv sync --group worker` resolves from, so every pin here
# already passed uv's own resolver as one consistent graph; there is no
# separate "Hermes's lock" being merged in afterward; the export below.
UV_FROZEN=1 uv export --no-hashes --no-default-groups --group worker \
  --format requirements-txt > "$REQS_FILE.raw"
# Strip the two editable/local-path entries: `-e .` (jones_daemon itself —
# built as a real wheel below instead) and `-e <hermes-agent path>` (source
# tree via git-archive below instead, per the module docstring).
grep -v '^-e ' "$REQS_FILE.raw" | grep -v '^#' > "$REQS_FILE"
rm -f "$REQS_FILE.raw"

rm -rf dist
UV_FROZEN=1 uv build --wheel >/dev/null
WHEEL="$(ls dist/jones_daemon-*.whl)"

# `--python-platform`/`--python <version>` (not a path): a pure resolve+
# download of prebuilt wheels for the TARGET architecture, needing no local
# interpreter of that architecture (spike 02 §1's whole point about
# python-build-standalone: no target-arch code execution required) — this is
# what makes producing the x64 bundle on this arm64 host possible AT ALL,
# but only for dependencies that actually publish an x64 wheel for the
# pinned version. **Known real failure on this host (see docs/release.md
# §2.2, docs/design/00-foundation.md §3.1's addendum)**: `cryptography==50.0.0`
# (daemon's exact-pinned dependency) ships zero macOS x86_64 wheels on PyPI
# for this version — `uv` silently falls back to building it from source
# (maturin/cargo), which then fails to cross-compile on an arm64 host with no
# x86_64 Rust target installed. This is a real gap, not a benign warning; the
# command below is left to fail loudly (not caught/retried) so that failure
# is never silently swallowed into a broken bundle.
uv pip install \
  --target "$SITE_PACKAGES" \
  --python-platform "$UV_PLATFORM" \
  --python 3.12 \
  -r "$REQS_FILE" \
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
