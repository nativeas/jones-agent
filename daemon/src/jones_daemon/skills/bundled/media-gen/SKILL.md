---
name: media-gen
description: "How to ask Hermes's own image_generate / text_to_speech / video_generate tools for an image, a voice clip, or a video — and which Key each provider needs."
version: 0.1.0
author: Jones
license: MIT
---

# Media Generation

This skill is **documentation only** — it ships no script of its own. Jones deliberately reuses Hermes's own built-in `image_generate` / `text_to_speech` / `video_generate` tools for media generation (docs/design/04-w5-interfaces.md §0: "不在 daemon 里写渲染代码") rather than re-implementing image/audio/video rendering. When one of these tools is present in your tool schema, call it directly — this file just tells you which one to reach for and which environment variable has to be set first.

## ⚠️ Known gap — read this before promising the user anything

As of this Issue (#21), **an ordinary Jones session cannot actually reach `image_generate` / `text_to_speech` / `video_generate` through normal chat tool-calling**, independent of any Key being configured. Source-verified against the installed `hermes-agent` checkout (`/Users/nativeas/.hermes/hermes-agent`, read-only):

- Jones spawns every worker through Hermes's ACP adapter, which hard-codes the session's tool bundle: `acp_adapter/session.py:393` calls `_expand_acp_enabled_toolsets(["hermes-acp"], mcp_server_names=...)` — the `["hermes-acp"]` literal is not read from `config.yaml` or any ACP protocol field (`NewSessionRequest` only carries `cwd`/`mcpServers`), so there is no lever Jones's daemon can pull to add another built-in toolset to an ACP session's schema.
- `toolsets.py`'s `"hermes-acp"` toolset is `_CODING_TOOLS` minus `clarify`, and `_CODING_TOOLS` (`toolsets.py:69`) is itself `_core_without("image_generate", "text_to_speech", "cronjob_manage", "computer_use", *_HA_TOOLS, kanban=False)` — `image_generate`/`text_to_speech` are explicitly excluded from the coding posture, by name, at the source.
- `video_generate` never enters the picture at all: it isn't in `_HERMES_CORE_TOOLS` to begin with (`toolsets.py:9-40`) — it only exists as its own `"video_gen"` toolset (`toolsets.py`'s `TOOLSETS["video_gen"]`), which nothing in the ACP path ever selects.

So today, a user asking Jones to "generate an image" gets an Agent with no `image_generate` tool in its schema at all — not a permission problem, not a missing Key, a tool that was never assembled. This is the same class of finding `capabilities/registry.py`'s own module docstring documents for a different corner of the same ACP-hard-coding fact (worth reading if you're deciding how to close this gap) — **not something this Issue's file ownership (`skills/bundled/**`, one `capabilities/`-adjacent constant) can fix by itself**: closing it needs either a change on Hermes's side (an ACP-session toolset override lever, which `hermes-agent` doesn't have today and is out of this repo) or a Jones-side alternative invocation path (e.g. shelling out to `hermes`'s own CLI in a one-shot, non-ACP mode) that nobody has scoped or reviewed yet. Flagged here, and in this branch's PR report, as an open item for the controller — **not silently patched over**.

What this Skill still gets you today: the moment any of the three tools DOES show up in a session's schema (an ACP toolset lever lands later, or you're checking with a real Hermes checkout directly, e.g. this repo's own `JONES_E2E`-gated tests), the instructions below are accurate and ready to use without further research.

## image_generate

Toolset `image_gen` (`toolsets.py`, provider FAL.ai). Call it with a `prompt` (and, for edits, `image_url`/`reference_image_urls`); it returns JSON `{"success", "image": <url>, ...}` — the `image` field is a hosted URL, not a local path, so if the user needs a local file, `web_extract`/`terminal curl` it down yourself.

**Key**: `FAL_KEY` (a direct FAL.ai API key), OR select the managed "nous" gateway via `hermes tools` (no separate key needed, subscription-gated) — see `tools/image_generation_tool.py::_resolve_managed_fal_gateway`. No Key and no managed gateway → the tool call itself returns a `provider_error`-shaped JSON body (never a crash); surface it to the user as "image generation isn't configured", don't retry.

## text_to_speech

Toolset `tts`. Default provider is **Edge TTS — free, no Key at all** (`DEFAULT_EDGE_VOICE`, `tools/tts_tool_providers.py`), so this one often just works out of the box. Higher-quality providers need their own Key, selected via `hermes tools` → Voice:

| Provider | Env var |
|---|---|
| ElevenLabs | `ELEVENLABS_API_KEY` |
| xAI | `XAI_API_KEY` |
| MiniMax | `MINIMAX_API_KEY` (or `MINIMAX_CN_API_KEY` for the CN region endpoint) |
| Mistral | `MISTRAL_API_KEY` |
| Gemini | `GEMINI_API_KEY` |
| OpenAI / DeepInfra | `OPENAI_API_KEY` |

Call with `text` (and optionally `voice`/`speed`/provider-specific overrides); it writes a real audio file and returns its path — that path IS local, hand it straight to the user.

## video_generate

Toolset `video_gen`. Unlike the other two, Hermes ships **no in-tree provider at all** (`tools/video_generation_tool.py`'s own module docstring: "Ships no in-tree provider: enable a plugin and select it in `hermes tools` → Video Generation") — `check_video_generation_requirements()` returns `True` only once the user has installed AND selected a `plugins/video_gen/<name>/` provider. Which env var it needs depends entirely on which plugin the user picked (an xAI-backed plugin needs `XAI_API_KEY`, same as xAI TTS/edit/extend). If no provider is configured, don't guess a Key name — tell the user video generation needs a provider set up first via `hermes tools`, same honest-failure shape as the other two.

## Provider Keys generally

Every Key above is B's provider/vault mechanism's job to store (`docs/design/*`'s BYOK sections) — this Skill only needs to know the **names** Hermes itself reads, not how they get into the environment. A tool call that comes back `provider_error`/`provider_auth` for a missing/invalid Key is the honest failure this system is built to show (N's error panel, #22) — never fabricate a result or silently fall back to a different provider the user didn't ask for.
