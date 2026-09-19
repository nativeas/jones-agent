"""N06 — PRD 12.2 负面清单:

"出现遥测、崩溃上报、使用统计等任何形式的自动上报" —— 对应原则 11.3（"零遥测：
不上报任何使用数据、崩溃日志；如需诊断，用户手动导出日志包"），绝对不能发生。

全程抓包核实（G16，"安装 → 使用 1 小时 → 关闭，全程抓包"）需要真机，是手工项
（见 `docs/acceptance/v1.0/G16.md`）。这里做静态的那一半，防回归：daemon 与
desktop 源码里都不出现任何已知遥测/崩溃上报 SDK 的依赖或调用痕迹（Sentry、
Bugsnag、Mixpanel、Amplitude、PostHog、Segment、Google Analytics/gtag、
Datadog RUM）。命中即说明有人往依赖或代码里引入了这类 SDK，必须先在这里失败，
不必等到抓包才发现。
"""

from __future__ import annotations

import json
import re

from ._reuse import DAEMON_ROOT

_DESKTOP_ROOT = DAEMON_ROOT.parent / "apps" / "desktop"

_FORBIDDEN = re.compile(
    r"\b(sentry|bugsnag|mixpanel|amplitude|posthog|segment\.io|"
    r"google-analytics|gtag\(|datadog-rum|@datadog/browser-rum)\b",
    re.IGNORECASE,
)


def _scan_source_for_telemetry_sdks(root, suffixes) -> list[str]:
    hits = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in suffixes:
            continue
        if "node_modules" in path.parts or ".venv" in path.parts or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if _FORBIDDEN.search(text):
            hits.append(str(path))
    return hits


def test_n06_no_telemetry_sdk_in_daemon_source() -> None:
    hits = _scan_source_for_telemetry_sdks(DAEMON_ROOT / "src", {".py"})
    assert hits == [], f"found a telemetry/crash-reporting SDK reference: {hits}"


def test_n06_no_telemetry_sdk_in_desktop_source() -> None:
    hits = _scan_source_for_telemetry_sdks(_DESKTOP_ROOT / "src", {".ts", ".tsx"})
    assert hits == [], f"found a telemetry/crash-reporting SDK reference: {hits}"


def test_n06_no_telemetry_sdk_in_declared_dependencies() -> None:
    daemon_pyproject = (DAEMON_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert not _FORBIDDEN.search(daemon_pyproject), "daemon/pyproject.toml declares a telemetry SDK"

    desktop_package_json = json.loads((_DESKTOP_ROOT / "package.json").read_text(encoding="utf-8"))
    deps = {
        **desktop_package_json.get("dependencies", {}),
        **desktop_package_json.get("devDependencies", {}),
    }
    hits = [name for name in deps if _FORBIDDEN.search(name)]
    assert hits == [], f"apps/desktop/package.json declares a telemetry SDK: {hits}"
