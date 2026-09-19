"""N02 — PRD 12.2 负面清单:

"任何路径（UI / 日志 / 回放 / 记忆 / 错误卡片 / 发给模型的 prompt）出现完整
API Key、Token 或凭据明文" —— 对应原则 5.2、10.4。与 G03 是同一份证据。

复用（不重复造）：见 `test_g03_key_never_reechoed.py`。
"""

from __future__ import annotations

from ._reuse import run_existing


def test_n02_key_never_plaintext_acceptance() -> None:
    run_existing(
        "tests/test_providers_methods.py::"
        "test_full_key_never_appears_in_logs_or_rpc_responses",
    )
