.PHONY: check check-daemon check-desktop
check: check-daemon check-desktop
check-daemon:
	cd daemon && uv run ruff check . && uv run pytest -q
check-desktop:
	cd apps/desktop && pnpm -s lint && pnpm -s typecheck && pnpm -s test
