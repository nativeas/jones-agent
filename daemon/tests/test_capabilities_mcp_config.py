"""Tests for `capabilities/mcp_config.py` — Jones `mcp.json` entry -> Hermes
`config.yaml` `mcp_servers:` shape (Issue #17, FR13)."""

from __future__ import annotations

from jones_daemon.capabilities.mcp_config import (
    hermes_mcp_server_config,
    hermes_mcp_servers_dict,
)


def test_stdio_entry_converts_to_command_args_env():
    result = hermes_mcp_server_config(
        {"name": "echo", "transport": "stdio", "command": "python3",
         "args": ["server.py", "--flag"], "env": {"FOO": "bar"}}
    )
    assert result == ("echo", {"command": "python3", "args": ["server.py", "--flag"],
                                "env": {"FOO": "bar"}})


def test_stdio_entry_without_explicit_transport_is_inferred_from_command():
    result = hermes_mcp_server_config({"name": "echo", "command": "python3", "args": []})
    assert result is not None
    assert result[1] == {"command": "python3", "args": [], "env": {}}


def test_http_entry_converts_to_url_headers():
    result = hermes_mcp_server_config(
        {"name": "docs", "transport": "http", "url": "https://example.com/mcp",
         "headers": {"Authorization": "Bearer x"}}
    )
    assert result == ("docs", {"url": "https://example.com/mcp",
                                "headers": {"Authorization": "Bearer x"}})


def test_sse_transport_flag_is_preserved():
    result = hermes_mcp_server_config(
        {"name": "legacy", "transport": "sse", "url": "https://example.com/sse"}
    )
    assert result[1]["transport"] == "sse"


def test_disabled_entry_is_dropped():
    assert hermes_mcp_server_config({"name": "echo", "command": "x", "enabled": False}) is None


def test_malformed_entries_are_dropped_not_raised():
    assert hermes_mcp_server_config({}) is None
    assert hermes_mcp_server_config({"name": ""}) is None
    assert hermes_mcp_server_config({"name": "x"}) is None  # neither command nor url
    assert hermes_mcp_server_config("not-a-dict") is None  # type: ignore[arg-type]


def test_hermes_mcp_servers_dict_merges_by_name_last_wins():
    servers = hermes_mcp_servers_dict(
        [
            {"name": "echo", "command": "a"},
            {"name": "docs", "url": "https://x"},
            {"name": "echo", "command": "b"},  # duplicate name -> overrides
            {"name": "broken"},  # dropped
        ]
    )
    assert servers == {
        "echo": {"command": "b", "args": [], "env": {}},
        "docs": {"url": "https://x", "headers": {}},
    }


def test_hermes_mcp_servers_dict_of_none_is_empty():
    assert hermes_mcp_servers_dict(None) == {}
