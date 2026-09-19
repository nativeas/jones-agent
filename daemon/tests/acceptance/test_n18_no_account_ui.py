"""N18 — PRD 12.2 负面清单:

"出现注册 / 登录 / 订阅 / 付费墙" —— 对应原则 5.2，绝对不能发生。与 G01（全新
机器安装到完成首个 Turn，不出现注册/登录/订阅任何入口）是同一实质要求，G01
本身需要真机走一遍（见 `docs/acceptance/v1.0/G01.md`），但"代码里压根没有这类
UI 入口"这一半是可以静态验证的、不需要真机。

静态检查（防回归）：`apps/desktop/src/renderer/` 的 UI 源码里不出现账号/付费
相关字样（"注册"、"登录"、"付费订阅"、`sign up`、`sign in`、`log in`、`paywall`、
`billing`、`subscription plan`、`checkout`）——特意不检查裸的
`subscription`/`subscribe`：这两个词在代码里合法地大量出现在
`session.subscribe`/`unsubscribe`（RPC 通知订阅机制，与付费订阅无关），检查
裸词会把每一处通知订阅都当成误报。
"""

from __future__ import annotations

import re

from ._reuse import DAEMON_ROOT

_RENDERER_SRC = DAEMON_ROOT.parent / "apps" / "desktop" / "src" / "renderer" / "src"

_FORBIDDEN_PATTERNS = [
    re.compile(r"注册"),
    re.compile(r"登录"),
    re.compile(r"付费"),
    re.compile(r"订阅制|付费订阅|订阅计划"),
    re.compile(r"\bsign[\s-]?up\b", re.IGNORECASE),
    re.compile(r"\bsign[\s-]?in\b", re.IGNORECASE),
    re.compile(r"\blog[\s-]?in\b", re.IGNORECASE),
    re.compile(r"\bpaywall\b", re.IGNORECASE),
    re.compile(r"\bbilling\b", re.IGNORECASE),
    re.compile(r"\bsubscription\s+plan\b", re.IGNORECASE),
    re.compile(r"\bpaid\s+subscription\b", re.IGNORECASE),
    re.compile(r"\bcheckout\b", re.IGNORECASE),
]


def test_n18_no_account_or_paywall_ui_in_renderer_source() -> None:
    assert _RENDERER_SRC.is_dir(), f"renderer src not found at {_RENDERER_SRC}"
    hits = []
    for path in _RENDERER_SRC.rglob("*"):
        if not path.is_file() or path.suffix not in (".ts", ".tsx", ".css"):
            continue
        if "__tests__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(_RENDERER_SRC.parent.parent.parent)
        for pattern in _FORBIDDEN_PATTERNS:
            if pattern.search(text):
                hits.append(f"{rel}: {pattern.pattern!r}")
    assert hits == [], f"found account/paywall-shaped UI text: {hits}"
