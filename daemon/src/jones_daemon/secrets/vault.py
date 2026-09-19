"""Encrypted credential vault (docs/design/01-w2-interfaces.md §3, PRD §10.4/G03/N02).

One file, `<user_root>/secrets/vault.enc`: a JSON envelope whose `ciphertext` is the whole
plaintext entry-map (`{name: secret_value}`) encrypted with AES-256-GCM (`cryptography`). The
32-byte data key never touches disk in this repo: it lives in the macOS Keychain (via `keyring`,
item `jones-agent/vault-key`, created on first use) — except under `JONES_VAULT_KEY`, an
explicit test-only gate (base64, 32 bytes) that skips the Keychain entirely so tests never
prompt for Keychain access or depend on a backend being available in CI (docs/design/
01-w2-interfaces.md §3: "Linux CI 用 keyrings.alt 文件后端或环境变量 JONES_VAULT_KEY 门控，
测试用后者").

Every path that can render a secret readable (logs, RPC responses, error messages) must never
carry the full value — see `providers/methods.py`'s `key_hint` (last 4 chars only) and the
grep-based regression test in `tests/test_providers_methods.py`. This module itself never logs
a decrypted value.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import secrets as _secrets
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_SERVICE = "jones-agent"
_ACCOUNT = "vault-key"
_KEY_BYTES = 32  # AES-256
_NONCE_BYTES = 12  # AES-GCM standard nonce size
_ENV_VAR = "JONES_VAULT_KEY"


class VaultError(RuntimeError):
    """Raised on any vault failure (missing/invalid data key, corrupt file, decrypt failure).

    DEV.md 工程原则 #4 (诚实失败): a vault that can't produce a trustworthy answer raises,
    never returns a default or silently drops the entry.
    """


def _decode_data_key(raw: str, *, source: str) -> bytes:
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise VaultError(f"{source}: vault data key is not valid base64") from exc
    if len(key) != _KEY_BYTES:
        raise VaultError(
            f"{source}: vault data key must decode to {_KEY_BYTES} bytes (AES-256), got {len(key)}"
        )
    return key


def _resolve_data_key() -> bytes:
    """`JONES_VAULT_KEY` (tests/CI) → macOS Keychain (real daemon), creating the Keychain item
    on first use. Never silently falls back between the two — a set-but-invalid env var is a
    hard error, not a quiet detour to the Keychain.
    """
    env_val = os.environ.get(_ENV_VAR)
    if env_val:
        return _decode_data_key(env_val, source=_ENV_VAR)

    try:
        import keyring
    except ImportError as exc:
        raise VaultError(
            "no vault data key available: JONES_VAULT_KEY is unset and the `keyring` package "
            "is not installed"
        ) from exc

    try:
        existing = keyring.get_password(_SERVICE, _ACCOUNT)
    except Exception as exc:  # noqa: BLE001 - keyring backends raise their own exception types
        raise VaultError(f"keyring backend failed to read {_SERVICE}/{_ACCOUNT}: {exc}") from exc
    if existing:
        return _decode_data_key(existing, source="macOS Keychain")

    new_key = _secrets.token_bytes(_KEY_BYTES)
    try:
        keyring.set_password(_SERVICE, _ACCOUNT, base64.b64encode(new_key).decode("ascii"))
    except Exception as exc:  # noqa: BLE001 - see above
        raise VaultError(f"keyring backend failed to write {_SERVICE}/{_ACCOUNT}: {exc}") from exc
    return new_key


class Vault:
    """Encrypted key-value store for provider API keys (one vault.enc per user_root).

    The data key is resolved lazily, on first `get`/`set`/`delete`/`names` call — not at
    construction — so building a `Vault` instance never touches the Keychain or requires
    `JONES_VAULT_KEY` unless it's actually used (mirrors `paths.py`'s "create on first access").
    """

    def __init__(self, path: Path, *, data_key: bytes | None = None) -> None:
        self._path = path
        self._data_key = data_key

    def _key(self) -> bytes:
        if self._data_key is None:
            self._data_key = _resolve_data_key()
        return self._data_key

    def _read_entries(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            envelope = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VaultError(
                f"vault file {self._path} is unreadable or not valid JSON: {exc}"
            ) from exc
        try:
            nonce = base64.b64decode(envelope["nonce"], validate=True)
            ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
        except (KeyError, TypeError, binascii.Error, ValueError) as exc:
            raise VaultError(f"vault file {self._path} has a malformed envelope: {exc}") from exc
        try:
            plaintext = AESGCM(self._key()).decrypt(nonce, ciphertext, None)
        except InvalidTag as exc:
            raise VaultError(
                f"vault file {self._path} failed to decrypt — wrong data key or corrupt file"
            ) from exc
        try:
            entries = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VaultError(f"vault file {self._path} decrypted to invalid JSON: {exc}") from exc
        if not isinstance(entries, dict):
            raise VaultError(f"vault file {self._path} decrypted to a non-object payload")
        return entries

    def _write_entries(self, entries: dict[str, str]) -> None:
        nonce = os.urandom(_NONCE_BYTES)  # fresh nonce every write — AES-GCM never reuses one
        ciphertext = AESGCM(self._key()).encrypt(nonce, json.dumps(entries).encode("utf-8"), None)
        envelope = {
            "version": 1,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp_path = self._path.with_name(f".{self._path.name}.tmp-{os.getpid()}")
        tmp_path.write_text(json.dumps(envelope), encoding="utf-8")
        tmp_path.chmod(0o600)
        os.replace(tmp_path, self._path)  # atomic on the same filesystem

    def get(self, name: str) -> str | None:
        return self._read_entries().get(name)

    def set(self, name: str, value: str) -> None:
        entries = self._read_entries()
        entries[name] = value
        self._write_entries(entries)

    def delete(self, name: str) -> bool:
        """Remove `name`; returns whether it existed. No-op (no file write) if it didn't."""
        entries = self._read_entries()
        if name not in entries:
            return False
        del entries[name]
        self._write_entries(entries)
        return True

    def names(self) -> list[str]:
        return list(self._read_entries())

    def reset(self, entries: dict[str, str]) -> None:
        """Discard whatever is on disk — even if it's corrupt JSON or no longer decrypts with the
        current data key — and start a brand new vault containing exactly `entries`. Unlike
        `set`/`delete`, this never calls `_read_entries()`, so it works precisely when they can't
        (round 1 review: a vault that fails `_read_entries()` — corrupt file, or a Keychain data
        key that no longer matches, e.g. after `jones-agent/vault-key` was deleted or the machine
        was migrated without it — used to permanently lock `provider.set_key`/`delete_key` behind
        `VaultError` with no way back in). This is the explicit, opt-in recovery path callers use
        for that: every entry the old vault held (for every provider, not just the one being set)
        is unrecoverable and permanently gone after this call — callers must only reach this after
        telling the user so (see `providers/methods.py`'s `force` param).
        """
        self._write_entries(entries)


def build_default_vault(secrets_dir: Path) -> Vault:
    """`Vault` at the standard `<user_root>/secrets/vault.enc` location.

    Takes `secrets_dir` (i.e. `paths.secrets_dir()`) rather than calling `paths` itself, so this
    module has no import-time dependency on `jones_daemon.paths` / `JONES_HOME` — callers that
    already resolved the path (tests, `providers/methods.py`) just pass it through.
    """
    return Vault(secrets_dir / "vault.enc")
