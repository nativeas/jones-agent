"""Real-Hermes proof for issue #21's `media-gen` bundled Skill (PRD 12.3 FR12:
"媒体生成 Skill 产出图 / 音 / 视频各一份可播放") — calls Hermes's own
`image_generate`/`text_to_speech`/`video_generate` tool FUNCTIONS directly,
same "prove the plumbing with real Hermes code, no daemon reimplementation"
approach `test_skills_hermes_e2e.py` already established for the Skill-
discovery half of this file's own docstring.

## Why direct function calls, not a live ACP session

`skills/bundled/media-gen/SKILL.md`'s own "已知限制" section documents a
source-verified finding: an ACP-launched Jones worker's `enabled_toolsets` is
hard-coded (`acp_adapter/session.py:393`) and never includes `image_generate`/
`text_to_speech`/`video_generate` — so there is no live-session path to
exercise today, only the underlying tool functions media-gen's instructions
describe. This test proves those functions behave exactly as the Skill
documents (real files, real "no Key configured" failures) — it is deliberately
NOT a claim that a normal Jones chat session can reach them yet; see that
SKILL.md section and this branch's PR report for the open item.

Gated on `JONES_E2E=1` (docs/design/01-w2-interfaces.md §2), same convention
as every other `tests/integration/*_hermes_e2e.py` file; each of the three
media kinds additionally self-skips on its own real, honest signal (not an
env var Jones invented) when that kind's provider isn't configured on this
machine — `check_image_generation_requirements()` /
`check_video_generation_requirements()` are Hermes's OWN readiness checks
(same functions `image_generate`/`video_generate` use internally before
attempting a call); Edge TTS needs no Key at all, so the `text_to_speech`
case runs under plain `JONES_E2E=1` with no provider Key required.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("JONES_E2E"),
    reason="real-Hermes e2e: set JONES_E2E=1 (and run `uv sync --group worker`) to run",
)

tts_tool = pytest.importorskip(
    "tools.tts_tool", reason="hermes-agent not installed (`uv sync --group worker`)"
)
image_tool = pytest.importorskip("tools.image_generation_tool")
video_tool = pytest.importorskip("tools.video_generation_tool")


def test_text_to_speech_produces_a_playable_audio_file(tmp_path):
    """No provider Key needed — Edge TTS (`DEFAULT_EDGE_VOICE`) is the free
    default `text_to_speech` falls back to with no `hermes tools` selection
    made, so this is the one media-gen case JONES_E2E alone can exercise."""
    out = tmp_path / "media-gen-tts.mp3"
    raw = tts_tool.text_to_speech_tool(
        "Jones bundled media-gen skill, end to end test.", output_path=str(out)
    )
    result = json.loads(raw)
    assert result["success"] is True, result
    produced = Path(result["file_path"])
    assert produced.is_file()
    assert produced.stat().st_size > 0
    # MPEG audio (Edge TTS's default encoding) frame sync byte — a real,
    # decodable audio stream, not an empty/placeholder file.
    assert produced.read_bytes()[:2] == b"\xff\xf3" or produced.read_bytes()[:3] == b"ID3"


@pytest.mark.skipif(
    not image_tool.check_image_generation_requirements(),
    reason="no image_gen provider configured (FAL_KEY / managed gateway) on this machine",
)
def test_image_generate_produces_a_real_image(tmp_path):
    raw = image_tool.image_generate_tool(
        prompt="a small red circle on a white background, minimalist test image"
    )
    result = json.loads(raw)
    assert result["success"] is True, result
    assert isinstance(result["image"], str) and result["image"].startswith("http")


@pytest.mark.skipif(
    not video_tool.check_video_generation_requirements(),
    reason="no video_gen provider plugin installed+selected (`hermes tools`) on this machine",
)
def test_video_generate_produces_a_real_video():
    """`video_generate`'s own module docstring: Hermes ships no in-tree
    provider at all, so this case is expected to self-skip on every machine
    that hasn't separately installed a `plugins/video_gen/<name>/` provider —
    media-gen/SKILL.md documents this as the least-standardized of the three
    rather than pretending a default exists. `_handle_video_generate` (not a
    plain public `video_generate_tool` — video's dispatch takes a single
    `args` dict, unlike `image_generate_tool`/`text_to_speech_tool`'s keyword
    args, source-verified against `tools/video_generation_tool.py`) is the
    same function the model's tool-calling loop would invoke."""
    raw = video_tool._handle_video_generate(
        {"prompt": "a small red circle, minimalist test clip"}
    )
    result = json.loads(raw)
    assert result.get("success") is True, result
