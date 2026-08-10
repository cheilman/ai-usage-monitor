"""OpenRouter usage provider.

Unlike Claude and Gemini, OpenRouter publishes a small, *documented*, key-scoped usage
endpoint, so this provider needs no keychain, no OAuth dance, and no local log scraping:

    GET https://openrouter.ai/api/v1/key
    Authorization: Bearer <api key>

It returns the spending cap configured on *that key* (`limit` / `limit_remaining` /
`limit_reset`, all in USD, `null` when the key is uncapped) plus running totals (`usage`,
`usage_daily`, `usage_weekly`, `usage_monthly`) and the same figures for external BYOK
spend. See https://openrouter.ai/docs/api_reference/limits. Because it's a real, stable API
response rather than an internal format we're guessing at, every window it produces is
`Confidence.AUTHORITATIVE` -- there is no fallback source to chain to.

"Usage by API key(s)" (plural) is the point of this provider: a user may hold several keys
(a personal one, a project's, a CI token, ...) and wants all of them at a glance rather than
re-running this tool once per key with a different environment. There is also no config file
needed for the common case, mirroring Claude's `ANTHROPIC_API_KEY`: every environment
variable named `OPENROUTER_API_KEY` (-> label "default") or `OPENROUTER_API_KEY_<LABEL>`
(-> label "<label>") is discovered automatically (see `config._discover_openrouter_keys`)
and reported as its own row.

Each key is queried independently and gets its own `SourceAttempt`, named after its label:
one revoked or misconfigured key must not hide the others, and `doctor` should be able to
say exactly which key failed and why.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime

from ai_usage_monitor import __version__
from ai_usage_monitor.config import OpenRouterConfig, OpenRouterKeyConfig
from ai_usage_monitor.models import (
    Confidence,
    ProviderSnapshot,
    SourceAttempt,
    SourceOutcome,
    UsageWindow,
)

KEY_URL = "https://openrouter.ai/api/v1/key"
SOURCE_NAME = "GET /api/v1/key"

NO_KEYS_REMEDIATION = (
    "Set OPENROUTER_API_KEY (or OPENROUTER_API_KEY_<LABEL> for each additional key you "
    "want to track), then re-run."
)
INVALID_KEY_REMEDIATION = (
    "Check that the key is still valid at https://openrouter.ai/settings/keys, or that "
    "the environment variable for this label holds the right value."
)
SCHEMA_DRIFT_REMEDIATION = (
    "The endpoint replied but not in the shape this provider expects; check "
    "https://openrouter.ai/docs/api_reference/limits for what changed."
)


class UsageSourceError(RuntimeError):
    """The endpoint was reachable-in-principle but didn't return usable data."""


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _slug(label: str) -> str:
    """Machine-safe suffix for a per-key window key, e.g. "my key" -> "my_key"."""
    cleaned = "".join(char if char.isalnum() else "_" for char in label.lower())
    return "_".join(part for part in cleaned.split("_") if part) or "key"


def fetch_key_usage(api_key: str, timeout: float = 10.0) -> dict:
    """GET the per-key usage document. Raises UsageSourceError with a secret-free message."""
    request = urllib.request.Request(
        KEY_URL,
        method="GET",
        headers={
            "Authorization": f"Bearer {api_key}",
            "accept": "application/json",
            "user-agent": f"ai-usage-monitor/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        hint = " -- the key may be invalid, disabled, or revoked" if exc.code in (401, 403) else ""
        raise UsageSourceError(f"{KEY_URL} returned HTTP {exc.code}{hint}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UsageSourceError(f"{KEY_URL} unreachable: {exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UsageSourceError(f"{KEY_URL} returned non-JSON body ({exc.msg})") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise UsageSourceError(f"{KEY_URL} returned no 'data' object -- the payload shape may have changed")
    return payload["data"]


def parse_key_usage(data: dict, label: str) -> tuple[list[UsageWindow], list[str]]:
    """Pure payload -> (windows, notes) for one key. Golden-fixture tested, no I/O."""
    slug = _slug(label)
    prefix = "" if label == "default" else f"{label} "

    limit = _as_float(data.get("limit"))
    limit_remaining = _as_float(data.get("limit_remaining"))
    usage = _as_float(data.get("usage")) or 0.0

    windows: list[UsageWindow] = []
    if limit is not None and limit_remaining is not None:
        # limit/limit_remaining describe the *current-period* cap; usage below is all-time,
        # so the two are deliberately not conflated into one number.
        reset = data.get("limit_reset")
        windows.append(
            UsageWindow(
                key=f"credit_limit_{slug}",
                label=f"{prefix}credit limit",
                unit="dollars",
                used=round(max(0.0, limit - limit_remaining), 6),
                limit=limit,
                reset_at=None,
                confidence=Confidence.AUTHORITATIVE,
                source=SOURCE_NAME,
                note=f"limit resets {reset}" if reset else "limit never resets",
            )
        )
    else:
        # No per-key cap: there is nothing to compute a percentage against, but the usage
        # total itself is still a real, live number, so it's reported (limit=None renders
        # as "-"/"?" rather than a guessed cap).
        windows.append(
            UsageWindow(
                key=f"usage_{slug}",
                label=f"{prefix}usage (no cap)",
                unit="dollars",
                used=usage,
                limit=None,
                reset_at=None,
                confidence=Confidence.AUTHORITATIVE,
                source=SOURCE_NAME,
                note="this key has no spending limit configured",
            )
        )

    notes: list[str] = []
    daily = _as_float(data.get("usage_daily")) or 0.0
    weekly = _as_float(data.get("usage_weekly")) or 0.0
    monthly = _as_float(data.get("usage_monthly")) or 0.0
    tier = "free tier" if data.get("is_free_tier") else "paid tier"
    notes.append(
        f"{label}: ${daily:.2f} today / ${weekly:.2f} this week / ${monthly:.2f} this month "
        f"(${usage:.2f} all-time, {tier})"
    )

    byok = _as_float(data.get("byok_usage")) or 0.0
    if byok:
        notes.append(f"{label}: ${byok:.2f} all-time BYOK (bring-your-own-key) usage")

    return windows, notes


def _fetch_key_source(
    key: OpenRouterKeyConfig,
) -> tuple[list[UsageWindow], list[str], SourceAttempt]:
    """Fetch and parse one key's usage, timing it into a SourceAttempt either way.

    Not a plain `run_source`-shaped source (see providers/base.py): a successful fetch also
    carries per-key `notes`, which the shared `(windows, outcome, detail, remediation)` shape
    has no room for -- the same reason Claude's `_try_live_usage` doesn't use it either.
    """
    started = time.perf_counter()
    name = f"{SOURCE_NAME} ({key.label})"

    def _attempt(outcome: SourceOutcome, detail: str, remediation: str | None) -> SourceAttempt:
        return SourceAttempt(
            name=name,
            outcome=outcome,
            detail=detail,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            remediation=remediation,
        )

    try:
        data = fetch_key_usage(key.api_key)
    except UsageSourceError as exc:
        return [], [], _attempt(SourceOutcome.ERROR, str(exc), INVALID_KEY_REMEDIATION)

    windows, notes = parse_key_usage(data, key.label)
    if not windows:
        return (
            [],
            [],
            _attempt(
                SourceOutcome.EMPTY,
                f"{KEY_URL} succeeded but returned no usable usage fields",
                SCHEMA_DRIFT_REMEDIATION,
            ),
        )

    return (
        windows,
        notes,
        _attempt(SourceOutcome.OK, f"{len(windows)} window(s), label={key.label!r}", None),
    )


class OpenRouterProvider:
    name = "openrouter"

    def __init__(self, config: OpenRouterConfig):
        self.config = config

    def fetch(self) -> ProviderSnapshot:
        now = datetime.now(UTC)
        snapshot = ProviderSnapshot(provider=self.name, fetched_at=now)

        if not self.config.keys:
            snapshot.attempts.append(
                SourceAttempt(
                    name=SOURCE_NAME,
                    outcome=SourceOutcome.NO_CREDENTIAL,
                    detail="no OPENROUTER_API_KEY (or OPENROUTER_API_KEY_<LABEL>) "
                    "environment variable is set",
                    duration_ms=0.0,
                    remediation=NO_KEYS_REMEDIATION,
                )
            )
            snapshot.errors.append("no OpenRouter API key configured; nothing to report")
            return snapshot

        for key in self.config.keys:
            windows, notes, attempt = _fetch_key_source(key)
            snapshot.attempts.append(attempt)
            if attempt.ok:
                snapshot.windows.extend(windows)
                snapshot.notes.extend(notes)
            else:
                snapshot.errors.append(f"OpenRouter key {key.label!r}: {attempt.detail}")

        return snapshot
