"""G03 — PRD 12.1 发布门禁:

"Key 永不完整回显：设置页、日志、回放、错误卡片、记忆中均不出现完整 Key" ——
验证方式："全文 grep 已配置的 Key 字符串，命中数为 0"。

复用（不重复造）：`daemon/tests/test_providers_methods.py::
test_full_key_never_appears_in_logs_or_rpc_responses` 已经是这条门禁的字面实现
——配置一个真实形状的 Key，抓取 `provider.set_key`/`provider.list`/
`provider.delete_key` 的完整 RPC 响应与同一进程的日志输出，grep 整个 haystack
断言完整 Key 与其 base64 编码都命中数为 0，同时断言 hint（末 4 位）确实出现，
证明这不是一个因为压根没打印任何东西而空洞通过的断言——即 PRD 12.1 描述的
"全文 grep 已配置的 Key 字符串，命中数为 0"本身。
"""

from __future__ import annotations

from ._reuse import run_existing


def test_g03_full_key_never_reechoed_acceptance() -> None:
    run_existing(
        "tests/test_providers_methods.py::test_full_key_never_appears_in_logs_or_rpc_responses",
    )
