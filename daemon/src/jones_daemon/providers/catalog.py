"""Static six-vendor catalog: Jones vendor name → Hermes provider config (docs/design/
01-w2-interfaces.md §3, source-checked against hermes-agent commit
ee4452991d17534aa561f31ee55596d082aa94e7 — see that file's appended mapping table for the
per-field evidence).

This is deliberately a plain static table, not a discovery mechanism: PRD 6.5 / Issue #7 v1 scope
is six named vendors, and Hermes adds new providers as adapters without touching its core, so
there's nothing here that benefits from being pluggable yet (DEV.md 工程原则 #6: 不写「以后可能
用到」的代码). Model ids in `models` are a seed catalog, not a live fetch — real ids drift often
(hermes-agent's own model_tools/models.py resolve against a live models.dev catalog); refreshing
this list is out of scope for W2 and left to W5 (BYOK 六厂商收口, per the Issue's own note).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class VendorSpec:
    vendor: str  # Jones-facing name (matches `providers.name` in SQLite)
    hermes_provider: str  # value written to `model.provider` in the worker's config.yaml
    default_base_url: str
    default_model: str
    models: tuple[str, ...]
    key_env: str | None = None  # env var Hermes's built-in PROVIDER_REGISTRY reads the key from
    base_url_env: str | None = None  # Hermes env var overriding the base URL, if ever needed
    # True only for vendors with no entry in Hermes's built-in PROVIDER_REGISTRY (i.e. Ollama):
    # these need an explicit `providers.<hermes_provider>` block in config.yaml (the "v12+
    # providers shape", hermes_cli/config_providers.py) rather than relying on env-var
    # auto-detection.
    custom_provider: bool = field(default=False)


VENDORS: dict[str, VendorSpec] = {
    "anthropic": VendorSpec(
        vendor="anthropic",
        hermes_provider="anthropic",
        default_base_url="https://api.anthropic.com",
        default_model="claude-opus-4-6",
        models=("claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-6"),
        key_env="ANTHROPIC_API_KEY",
        base_url_env="ANTHROPIC_BASE_URL",
    ),
    "openai": VendorSpec(
        vendor="openai",
        hermes_provider="openai-api",
        default_base_url="https://api.openai.com/v1",
        default_model="gpt-5.4",
        models=("gpt-5.4", "gpt-5-mini", "gpt-5-codex"),
        key_env="OPENAI_API_KEY",
        base_url_env="OPENAI_BASE_URL",
    ),
    "deepseek": VendorSpec(
        vendor="deepseek",
        hermes_provider="deepseek",
        default_base_url="https://api.deepseek.com/v1",
        default_model="deepseek-chat",
        models=("deepseek-chat", "deepseek-reasoner"),
        key_env="DEEPSEEK_API_KEY",
        base_url_env="DEEPSEEK_BASE_URL",
    ),
    "qwen": VendorSpec(
        # Hermes's registry id for this route is "alibaba" (DashScope); "qwen" is an alias that
        # resolves to it (hermes_cli/providers.py _ALIAS_GROUPS "alibaba": (..., "qwen", ...)) —
        # written out explicitly here rather than relied on, since Jones constructs config.yaml
        # directly and shouldn't depend on Hermes's alias table staying stable.
        vendor="qwen",
        hermes_provider="alibaba",
        default_base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        default_model="qwen3-max",
        models=("qwen3-max", "qwen3-coder"),
        key_env="DASHSCOPE_API_KEY",
        base_url_env="DASHSCOPE_BASE_URL",
    ),
    "gemini": VendorSpec(
        vendor="gemini",
        hermes_provider="gemini",
        default_base_url="https://generativelanguage.googleapis.com/v1beta",
        default_model="gemini-3-pro",
        models=("gemini-3-pro", "gemini-3-flash", "gemini-3.7-flash"),
        # GOOGLE_API_KEY and GEMINI_API_KEY are both accepted (PROVIDER_REGISTRY["gemini"]);
        # GOOGLE_API_KEY is checked first, so it's the one Jones writes into env.
        key_env="GOOGLE_API_KEY",
        base_url_env="GEMINI_BASE_URL",
    ),
    "ollama": VendorSpec(
        # No key_env: Ollama is not in Hermes's PROVIDER_REGISTRY at all — "ollama" is an alias
        # (hermes_cli/providers.py _ALIAS_GROUPS "custom": ("ollama",)) that resolves to the
        # generic "custom" OpenAI-compatible transport, configured via a `providers.ollama` block
        # in config.yaml, not an env-var-keyed registry row. Usually keyless (local server); an
        # optional key is still supported (see resolver.py) for a proxied/authenticated setup.
        vendor="ollama",
        hermes_provider="ollama",
        default_base_url="http://localhost:11434/v1",
        default_model="",
        models=(),
        custom_provider=True,
    ),
}

# Deterministic tie-break order when `ProviderResolver.resolve(None)` must pick a "user default"
# among multiple configured providers (see resolver.py `_default_configured_vendor`). Anthropic
# first mirrors the product's own stated default (PRD/Issue #7: "W1 先通一家（Anthropic）").
VENDOR_PRIORITY: tuple[str, ...] = ("anthropic", "openai", "deepseek", "qwen", "gemini", "ollama")

assert set(VENDOR_PRIORITY) == set(VENDORS)
