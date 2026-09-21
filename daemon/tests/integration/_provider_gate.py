"""Shared gate for the real-Hermes e2e tests.

Jones is BYOK across six vendors (`providers/catalog.py`), so gating these on
`ANTHROPIC_API_KEY` specifically meant a machine configured with any other
vendor silently skipped them — which is how `test_cap_files_g15`/`terminal`'s
"G09 无孤儿进程 / 五件套实跑" sat unverified for several waves on a box that had
a perfectly usable DeepSeek key. Pick whichever configured vendor we find, in
catalog order, and hand callers the `model:` block to append to the worker's
`config.yaml`.
"""

from __future__ import annotations

import os

from jones_daemon.providers.catalog import VENDORS


def configured_vendor() -> tuple[str, str] | None:
    """`(hermes_provider, model_id)` for the first vendor whose key env var is
    set in this environment, or `None` when no vendor is configured."""
    for spec in VENDORS.values():
        if not spec.key_env or not spec.default_model:
            continue
        if os.environ.get(spec.key_env):
            return spec.hermes_provider, spec.default_model
    return None


def model_config_block() -> str:
    vendor = configured_vendor()
    if vendor is None:  # pragma: no cover - callers gate on configured_vendor() first
        raise RuntimeError("no model provider configured")
    provider, model = vendor
    return f"model:\n  default: {model}\n  provider: {provider}\n"


NEEDS_REAL_MODEL_REASON = (
    "real-Hermes e2e: set JONES_E2E=1 and one vendor key from providers/catalog.py "
    "(ANTHROPIC_API_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY / DASHSCOPE_API_KEY / "
    "GOOGLE_API_KEY) to run"
)
