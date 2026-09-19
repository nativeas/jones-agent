"""`AcpClient` wire-level tests against `fake_acp_agent.py` (docs/design/
00-foundation.md §8.3, 01-w2-interfaces.md §2)."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from jones_daemon.kernel.acp_client import AcpClient, AcpProtocolError

_FAKE_AGENT = str(Path(__file__).parent / "fake_acp_agent.py")


async def _start_client(*, mode: str = "normal", on_request_permission=None):
    env = dict(os.environ)
    env["FAKE_ACP_MODE"] = mode
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        _FAKE_AGENT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    events: list[dict] = []

    async def _on_update(params):
        events.append(params)

    async def _default_permission(_params):
        return {"outcome": {"outcome": "selected", "optionId": "opt-allow-once"}}

    client = AcpClient(
        process.stdout, process.stdin,
        on_session_update=_on_update,
        on_request_permission=on_request_permission or _default_permission,
    )
    return client, process, events


async def _stop(client: AcpClient, process: asyncio.subprocess.Process) -> None:
    await client.close()
    if process.returncode is None:
        process.terminate()
        await process.wait()


async def test_initialize_and_new_session_roundtrip():
    client, process, _events = await _start_client()
    try:
        init_result = await client.initialize()
        assert init_result["protocolVersion"] == 1
        session = await client.new_session("/tmp")
        assert session["sessionId"] == "fake-session-1"
    finally:
        await _stop(client, process)


async def test_prompt_streams_message_delta_events():
    client, process, events = await _start_client()
    try:
        await client.initialize()
        session = await client.new_session("/tmp")
        response = await client.prompt(session["sessionId"], "hello there")
        assert response["stopReason"] == "end_turn"
        chunks = [
            e["update"]["content"]["text"]
            for e in events
            if e["update"]["sessionUpdate"] == "agent_message_chunk"
        ]
        assert chunks == ["Hel", "lo"]
    finally:
        await _stop(client, process)


async def test_prompt_with_tool_call_emits_start_and_update():
    client, process, events = await _start_client()
    try:
        await client.initialize()
        session = await client.new_session("/tmp")
        await client.prompt(session["sessionId"], "please USE_TOOL now")
        kinds = [e["update"]["sessionUpdate"] for e in events]
        assert "tool_call" in kinds
        assert "tool_call_update" in kinds
        completed = [
            e
            for e in events
            if e["update"]["sessionUpdate"] == "tool_call_update"
            and e["update"]["toolCallId"] == "demo-1"
        ]
        assert completed[0]["update"]["status"] == "completed"
    finally:
        await _stop(client, process)


async def test_request_permission_round_trip_is_answered_by_the_client():
    seen_requests = []

    async def _on_permission(params):
        seen_requests.append(params)
        return {"outcome": {"outcome": "selected", "optionId": "opt-allow-once"}}

    client, process, events = await _start_client(on_request_permission=_on_permission)
    try:
        await client.initialize()
        session = await client.new_session("/tmp")
        response = await client.prompt(session["sessionId"], "NEEDS_PERMISSION please")
        assert response["stopReason"] == "end_turn"
        assert len(seen_requests) == 1
        assert seen_requests[0]["toolCall"]["title"] == "risky_tool"
        final = [
            e
            for e in events
            if e["update"].get("toolCallId") == "perm-1"
            and e["update"]["sessionUpdate"] == "tool_call_update"
        ]
        assert final[0]["update"]["status"] == "completed"
    finally:
        await _stop(client, process)


async def test_cancel_notification_makes_the_in_flight_prompt_report_cancelled():
    async def _slow_permission(_params):
        # Deliberately slower than the sleep below, so the fake agent's prompt
        # handler thread is still blocked in `_wait_for_response` (i.e. the Turn
        # is genuinely still in flight) at the moment `cancel()` is sent.
        await asyncio.sleep(0.3)
        return {"outcome": {"outcome": "selected", "optionId": "opt-allow-once"}}

    client, process, _events = await _start_client(on_request_permission=_slow_permission)
    try:
        await client.initialize()
        session = await client.new_session("/tmp")
        prompt_task = asyncio.create_task(
            client.prompt(session["sessionId"], "NEEDS_PERMISSION slow")
        )
        await asyncio.sleep(0.1)
        await client.cancel(session["sessionId"])
        response = await prompt_task
        assert response["stopReason"] == "cancelled"
    finally:
        await _stop(client, process)


async def test_unsupported_client_method_gets_an_honest_error_not_a_hang():
    import json

    # Here the *fake agent* is the one calling back into the daemon for a
    # capability AcpClient.initialize() declares unsupported (00-foundation.md
    # §8.3) — it writes the daemon's answer to its own stderr so this test can
    # observe it without a second reader fighting AcpClient over stdout.
    client, process, _events = await _start_client(mode="call_unsupported_method")
    try:
        await client.initialize()
        await client.new_session("/tmp")
        stderr_line = await asyncio.wait_for(process.stderr.readline(), timeout=2)
        payload = json.loads(stderr_line)
        assert payload["fs_read_text_file_response"]["code"] == -32601
    finally:
        await _stop(client, process)


async def test_worker_stdout_closing_fails_pending_requests_honestly():
    client, process, _events = await _start_client(mode="exit_immediately")
    try:
        with pytest.raises(AcpProtocolError):
            await client.initialize()
    finally:
        await _stop(client, process)
