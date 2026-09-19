"""Issue #16 (FR10 深度调研) acceptance check: real Hermes `web_search` +
`web_extract`, driven exactly the way a worker's `invoke_tool()` would, produce
a citation URL set whose real-world reachability is >= 90% (PRD 12.3's literal
bar). Jones does not reimplement search/extract or the citation-grounding
workflow — DEV.md 工程原则 #1 ("能直接复用 Hermes 的...就复用") — `skills/research/
grounded-citations` (Hermes's own bundled skill, see report "调研结论") already
owns the ledger/citation mechanics; this test proves the two tools it's built
on actually work end to end and meet the acceptance bar, without needing a
model in the loop (same "drive the real tool directly, no LLM required"
approach as `test_cap_browser_e2e.py`).

Two independent things:
  - `test_web_search_results_meet_the_citation_reachability_bar`: real network,
    real Hermes, `JONES_E2E=1` gated. Review finding #4: 03-w4-interfaces.md §4
    literally says "测报告中的引用 URL 可达率" (the URLs a generated REPORT
    cites) — this branch has no report-generation feature to point that at, so
    this test measures the reachability of `web_search_tool`'s raw result URLs
    instead, as a documented downgrade of that acceptance criterion, not an
    equivalent of it. `grounded-citations`' own `Sources:` list is built from
    exactly these URLs, so the two sets should coincide in practice, but that
    equivalence isn't proven here.
  - `test_web_search_tool_returns_a_provider_error_shape_when_nothing_resolves`:
    no network needed — deterministically forces "no provider available" by
    monkeypatching Hermes's own resolution seam, to lock in the exact
    `{"success": false, "error": ...}` shape 03-w4-interfaces.md §4 says daemon
    must turn into an explicit `provider_error` card (not implemented by this
    branch — daemon-side RPC error mapping is out of `capabilities/browser.py`'s
    ownership, see report "没做什么") rather than fail silently. Controller
    ruling R-J5 (round-2 review): this test needs no network and must NOT sit
    behind the `JONES_E2E` gate the other two tests need — it's only gated on
    `hermes-agent` being importable at all (the same `_hermes_available()`
    check every test in this file already uses), same as CI-runnable unit
    tests elsewhere in this branch.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import urllib.error
import urllib.request

import pytest

_needs_jones_e2e = pytest.mark.skipif(
    not os.environ.get("JONES_E2E"),
    reason="needs real network: set JONES_E2E=1 (see module docstring)",
)


def _hermes_available() -> bool:
    try:
        import tools.web_tools  # noqa: F401
    except ImportError:
        return False
    return True


def _url_reachable(url: str, timeout_s: float = 8.0) -> bool:
    """Review findings #4/#13: the original `exc.code not in (0,)` check was
    vacuously true for every `HTTPError` (`.code` is never `0`), so 404/410/500
    dead links all counted as "reachable" and this test's PRD 12.3 90% bar
    could not fail on connection-level success. Controller ruling R-J5 (round-2
    review): count 2xx/3xx ONLY — no exception for a HEAD-rejecting 405/501 or
    a bot-blocked 403/429; PRD 12.3's bar is meant to catch real dead links, and
    a laxer definition of "reachable" than "the server answered success" makes
    the bar easier to pass than what it's supposed to measure."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "jones-agent-e2e/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return 200 <= resp.status < 400
    except urllib.error.HTTPError:
        return False
    except (urllib.error.URLError, OSError, ValueError):
        return False


@_needs_jones_e2e
@pytest.mark.skipif(
    not _hermes_available(), reason="hermes-agent not importable (uv sync --group worker)"
)
def test_web_search_results_meet_the_citation_reachability_bar():
    import tools.web_tools as wt

    result = json.loads(wt.web_search_tool("what is the capital of France", limit=8))
    assert result.get("success") is True, (
        f"web_search_tool failed — see report for which provider Key this environment "
        f"needed: {result}"
    )
    urls = [
        entry["url"]
        for entry in result.get("data", {}).get("web", [])
        if entry.get("url", "").startswith("http")
    ]
    assert len(urls) >= 3, f"expected at least 3 citation URLs, got {urls}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        reachable = list(pool.map(_url_reachable, urls))
    rate = sum(reachable) / len(reachable)
    assert rate >= 0.90, (
        f"citation reachability {rate:.0%} ({sum(reachable)}/{len(reachable)}) "
        f"is below PRD 12.3's 90% bar; urls={list(zip(urls, reachable, strict=True))}"
    )


@_needs_jones_e2e
@pytest.mark.skipif(
    not _hermes_available(), reason="hermes-agent not importable (uv sync --group worker)"
)
def test_web_extract_reads_real_page_content():
    """The other half of FR10 ("搜索 + 抓取"): `web_extract` on a known-stable
    URL actually returns page text, not just a search snippet. `web_extract_tool`
    is Hermes's async tool variant (`tools/web_tools.py` registers it with
    `is_async=True`) — run it to completion with `asyncio.run`, same as the
    concurrent tool-call path `invoke_tool()` itself uses."""
    import asyncio

    import tools.web_tools as wt

    result = json.loads(asyncio.run(wt.web_extract_tool(["https://example.com"])))
    # Real shape observed (not `{"success": ..., "data": ...}` like web_search_tool
    # — extract's top level is `{"results": [{"url", "title", "content", "error"}]}`,
    # per-URL `error` is the failure signal, not a top-level "success" key):
    entries = result.get("results") or []
    assert entries, result
    assert entries[0].get("error") is None, entries[0]
    content = " ".join(str(e.get("content", "")) for e in entries)
    assert "Example Domain" in content, f"expected real page content, got: {content[:300]!r}"


@pytest.mark.skipif(
    not _hermes_available(), reason="hermes-agent not importable (uv sync --group worker)"
)
def test_web_search_tool_returns_a_provider_error_shape_when_nothing_resolves(monkeypatch):
    """No network: force Hermes's own provider-resolution seam to report nothing
    available (mirrors what a real "no Key configured, keyless tier also
    unavailable" deployment looks like — see report "调研结论" for how this
    environment's real config normally avoids that path via Firecrawl's public
    keyless tier) and assert the tool returns `{"success": false, "error": ...}`,
    never raises, never returns a fabricated result."""
    import tools.web_tools as wt

    monkeypatch.setattr(wt, "_registered_web_provider", lambda backend: None)
    monkeypatch.setattr(wt, "_get_search_backend", lambda: "nonexistent-provider")

    result = json.loads(wt.web_search_tool("anything", limit=1))
    assert result["success"] is False
    assert isinstance(result["error"], str) and result["error"], result
