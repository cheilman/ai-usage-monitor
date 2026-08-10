"""Read-only access to the OAuth credentials the provider CLIs already store.

Strictly observational, per the design doc's credentials decision: we read the token
Claude Code wrote, and never refresh, rotate, or write it back. Another live process owns
that store, and racing it could log the user out of a working CLI mid-session. If the token
is expired we say so and fall through to a degraded source instead of trying to fix it.

Lookup order for Claude:

1. an explicit path (config `claude.credentials_file` or `$CLAUDE_CREDENTIALS_FILE`) --
   when set, it is the *only* thing consulted, so tests and headless boxes are deterministic
2. macOS keychain, service "Claude Code-credentials" (may raise a one-time access prompt)
3. `~/.claude/.credentials.json` -- where Linux installs keep the same JSON blob

Token values are never logged, never put in an error message, and never serialized.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

KEYCHAIN_SERVICE = "Claude Code-credentials"
DEFAULT_CREDENTIALS_FILE = Path("~/.claude/.credentials.json")
_KEYCHAIN_TIMEOUT_SECONDS = 10


class CredentialError(RuntimeError):
    """No usable credential. The message is user-facing and must not contain secrets."""


@dataclass(frozen=True)
class OAuthCredential:
    access_token: str
    expires_at: datetime | None
    subscription_type: str | None
    source: str

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= datetime.now(UTC)


def _parse_expires_at(raw: object) -> datetime | None:
    """Claude Code writes `expiresAt` as epoch milliseconds; tolerate seconds and ISO too."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        seconds = raw / 1000 if raw > 1e11 else float(raw)
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _credential_from_blob(raw: str, source: str) -> OAuthCredential:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CredentialError(f"{source}: credential blob is not valid JSON ({exc.msg})") from exc

    if not isinstance(data, dict):
        raise CredentialError(f"{source}: credential blob is not a JSON object")

    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        raise CredentialError(f"{source}: no 'claudeAiOauth' section -- schema may have changed")

    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token:
        raise CredentialError(f"{source}: no 'accessToken' in the credential blob")

    return OAuthCredential(
        access_token=token,
        expires_at=_parse_expires_at(oauth.get("expiresAt")),
        subscription_type=oauth.get("subscriptionType"),
        source=source,
    )


def _read_file(path: Path) -> str | None:
    try:
        return path.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CredentialError(f"{path}: could not be read ({exc.strerror})") from exc


def _read_keychain(service: str = KEYCHAIN_SERVICE) -> str | None:
    """Return the raw keychain blob, or None if this platform/entry can't provide one."""
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=_KEYCHAIN_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        return None
    except subprocess.SubprocessError as exc:
        raise CredentialError(
            f"keychain lookup for {service!r} failed: {type(exc).__name__}"
        ) from exc

    if result.returncode != 0:
        # 44 = entry not found; anything else is usually a denied access prompt.
        if result.returncode == 44:
            return None
        raise CredentialError(
            f"keychain lookup for {service!r} exited {result.returncode} "
            "(access denied? re-run and allow the prompt)"
        )
    return result.stdout.strip() or None


def load_claude_credential(credentials_file: Path | None = None) -> OAuthCredential:
    """Load Claude Code's OAuth credential, or raise CredentialError explaining why not."""
    explicit = credentials_file or (
        Path(os.environ["CLAUDE_CREDENTIALS_FILE"]).expanduser()
        if os.environ.get("CLAUDE_CREDENTIALS_FILE")
        else None
    )
    if explicit is not None:
        raw = _read_file(explicit)
        if raw is None:
            raise CredentialError(f"{explicit}: credentials file does not exist")
        return _credential_from_blob(raw, source=str(explicit))

    blob = _read_keychain()
    if blob is not None:
        return _credential_from_blob(blob, source=f"macOS keychain ({KEYCHAIN_SERVICE})")

    default_path = DEFAULT_CREDENTIALS_FILE.expanduser()
    raw = _read_file(default_path)
    if raw is not None:
        return _credential_from_blob(raw, source=str(default_path))

    where = (
        f"keychain service {KEYCHAIN_SERVICE!r} or {default_path}"
        if sys.platform == "darwin"
        else str(default_path)
    )
    raise CredentialError(f"no Claude Code OAuth credential found (looked in {where})")
