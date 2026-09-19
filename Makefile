.PHONY: check check-daemon check-desktop
check: check-daemon check-desktop
check-daemon:
	# UV_FROZEN=1: daemon/pyproject.toml's `worker` group (hermes-agent) points
	# `[tool.uv.sources]` at a machine-local editable checkout that most
	# machines don't have. Without --frozen/UV_FROZEN, uv still re-resolves the
	# full lock (all groups, installed or not) before running, which touches
	# that path and fails even though this target never installs `worker`. See
	# docs/design/01-w2-interfaces.md §2.2 and docs/DEV.md §2.2.
	cd daemon && UV_FROZEN=1 uv run ruff check . && UV_FROZEN=1 uv run pytest -q
check-desktop:
	cd apps/desktop && pnpm -s lint && pnpm -s typecheck && pnpm -s test
