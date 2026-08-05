"""Claude usage provider.

Ordered source chain, per the approved design (K-000076 §4.2). Sources are tried in order
and the first one that yields windows wins; each failure is recorded on the snapshot so
`--json` consumers can see *why* they got a degraded number instead of a real one.

1. **`GET https://api.anthropic.com/api/oauth/usage`** (primary, AUTHORITATIVE).
   The private endpoint Claude Code itself uses, authenticated with the OAuth access token
   already in the macOS keychain (service "Claude Code-credentials") or
   `~/.claude/.credentials.json`. Verified live during the design phase. It returns
   `five_hour` / `seven_day*` utilization percentages, `resets_at` per window, a
   pre-normalized `limits[]` array carrying `severity` and `is_active`, plus `plan`,
   `extra_usage` and `spend`. Utilization is already normalized against the account's real
   cap, so this path needs **no user-supplied limits at all**. We prefer `limits[]` because
   it is plan-shape-agnostic, and fall back to the named keys if it's absent.

2. **Local transcript scraping** (fallback, ESTIMATED at best).
   Claude Code writes one JSONL file per session under ~/.claude/projects/<project>/*.jsonl
   with token counts in `message.usage`. Summing the active rolling block (gap-based split,
   the heuristic the community `ccusage` tool uses) gives token *counts* but no plan cap --
   Anthropic publishes the cap nowhere local. So this path only produces a percentage if the
   user hand-configured `session_token_limit`/`weekly_token_limit`, and reports
   confidence UNAVAILABLE otherwise. It exists for non-macOS/headless boxes and for when the
   credential is missing or expired; it is never used while source 1 is working.

Separately (not part of the chain): if `ANTHROPIC_API_KEY` is set we read the
`anthropic-ratelimit-tokens-*` headers off the free `/v1/messages/count_tokens` endpoint.
Those are authoritative but describe *API-key* rate limits, which are a different thing from
subscription usage, so they are reported as their own clearly-labelled window and never
conflated with sources 1 or 2.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ai_usage_monitor import __version__
from ai_usage_monitor.config import ClaudeConfig
from ai_usage_monitor.credentials import (
    CredentialError,
    OAuthCredential,
    load_claude_credential,
)
from ai_usage_monitor.models import Confidence, ProviderSnapshot, UsageWindow

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages/count_tokens"
ANTHROPIC_VERSION = "2023-06-01"

OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
OAUTH_SOURCE = "GET /api/oauth/usage (Claude Code OAuth token)"
TRANSCRIPT_SOURCE = "local JSONL transcript scrape (fallback; ~/.claude/projects)"

# limits[].kind -> display label. Unknown kinds fall back to a prettified kind string, so a
# new plan shape shows up as an extra row rather than vanishing.
_LIMIT_LABELS = {
    "session": "5-hour session",
    "five_hour": "5-hour session",
    "weekly_all": "Weekly (all models)",
    "weekly_opus": "Weekly (Opus)",
    "weekly_sonnet": "Weekly (Sonnet)",
}

# Named top-level keys, used only when limits[] is missing/empty.
_NAMED_WINDOWS = (
    ("five_hour", "5-hour session"),
    ("seven_day", "Weekly (all models)"),
    ("seven_day_opus", "Weekly (Opus)"),
    ("seven_day_sonnet", "Weekly (Sonnet)"),
)


class UsageSourceError(RuntimeError):
    """A source was reachable-in-principle but didn't produce usable data."""


@dataclass
class _Event:
    timestamp: datetime
    tokens: int


def _parse_timestamp(raw: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw)
    except (ValueError, AttributeError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _money(value: object) -> tuple[float, str] | None:
    """Normalize a {"amount_minor": 1234, "currency": "USD", "exponent": 2} block."""
    if not isinstance(value, dict):
        return None
    minor = _as_float(value.get("amount_minor"))
    if minor is None:
        return None
    exponent = _as_float(value.get("exponent"))
    scale = 10 ** int(exponent) if exponent is not None else 100
    currency = value.get("currency") or "USD"
    return minor / scale, str(currency)


# --------------------------------------------------------------------------------------
# Source 1: the live OAuth usage endpoint
# --------------------------------------------------------------------------------------


def fetch_oauth_usage(credential: OAuthCredential, timeout: float = 10.0) -> dict:
    """GET the live usage document. Raises UsageSourceError with a secret-free message."""
    request = urllib.request.Request(
        OAUTH_USAGE_URL,
        method="GET",
        headers={
            "Authorization": f"Bearer {credential.access_token}",
            "anthropic-beta": OAUTH_BETA,
            "accept": "application/json",
            "user-agent": f"ai-usage-monitor/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        hint = " -- token may be expired; run `claude` once to refresh" if exc.code == 401 else ""
        raise UsageSourceError(f"{OAUTH_USAGE_URL} returned HTTP {exc.code}{hint}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UsageSourceError(f"{OAUTH_USAGE_URL} unreachable: {exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UsageSourceError(f"{OAUTH_USAGE_URL} returned non-JSON body ({exc.msg})") from exc

    if not isinstance(payload, dict):
        raise UsageSourceError(
            f"{OAUTH_USAGE_URL} returned a {type(payload).__name__}, not an object"
        )
    return payload


def _percent_window(
    label: str,
    percent: float,
    resets_at: object,
    *,
    severity: str | None = None,
    is_active: bool = False,
) -> UsageWindow:
    """Utilization is already normalized against the plan cap, so limit is a literal 100%."""
    return UsageWindow(
        label=label,
        unit="percent",
        used=percent,
        limit=100.0,
        reset_at=_parse_timestamp(resets_at) if isinstance(resets_at, str) else None,
        confidence=Confidence.AUTHORITATIVE,
        source=OAUTH_SOURCE,
        is_active=is_active,
        severity=severity,
    )


def _windows_from_limits(payload: dict) -> list[UsageWindow]:
    limits = payload.get("limits")
    if not isinstance(limits, list):
        return []

    windows: list[UsageWindow] = []
    for entry in limits:
        if not isinstance(entry, dict):
            continue
        percent = _as_float(entry.get("percent"))
        if percent is None:
            continue
        kind = str(entry.get("kind") or entry.get("group") or "usage")
        severity = entry.get("severity")
        windows.append(
            _percent_window(
                _LIMIT_LABELS.get(kind, kind.replace("_", " ").capitalize()),
                percent,
                entry.get("resets_at"),
                severity=str(severity) if severity else None,
                is_active=bool(entry.get("is_active")),
            )
        )
    return windows


def _windows_from_named_keys(payload: dict) -> list[UsageWindow]:
    windows: list[UsageWindow] = []
    for key, label in _NAMED_WINDOWS:
        block = payload.get(key)
        if not isinstance(block, dict):
            continue
        utilization = _as_float(block.get("utilization"))
        if utilization is None:
            continue
        windows.append(_percent_window(label, utilization, block.get("resets_at")))
    return windows


def _dollar_windows(payload: dict) -> list[UsageWindow]:
    """Credit/overage plans express the same windows in dollars; surface those too."""
    windows: list[UsageWindow] = []
    for key, label in _NAMED_WINDOWS:
        block = payload.get(key)
        if not isinstance(block, dict):
            continue
        used = _as_float(block.get("used_dollars"))
        limit = _as_float(block.get("limit_dollars"))
        if used is None or limit is None:
            continue
        windows.append(
            UsageWindow(
                label=f"{label} ($)",
                unit="dollars",
                used=used,
                limit=limit,
                reset_at=_parse_timestamp(block.get("resets_at", "")) or None,
                confidence=Confidence.AUTHORITATIVE,
                source=OAUTH_SOURCE,
            )
        )
    return windows


def _notes_from_payload(payload: dict) -> list[str]:
    notes: list[str] = []

    extra = payload.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled"):
        credits = _as_float(extra.get("used_credits")) or 0.0
        currency = extra.get("currency") or "USD"
        notes.append(f"extra usage on: {credits:.2f} {currency} of credits used")

    spend = payload.get("spend")
    if isinstance(spend, dict):
        used = _money(spend.get("used"))
        limit = _money(spend.get("limit"))
        if used and (used[0] > 0 or limit):
            tail = f" of {limit[0]:.2f} {limit[1]}" if limit else ""
            notes.append(f"spend this period: {used[0]:.2f} {used[1]}{tail}")

    return notes


def parse_oauth_usage(payload: dict) -> tuple[list[UsageWindow], str | None, list[str]]:
    """Pure payload -> (windows, plan, notes). Golden-fixture tested, no I/O."""
    windows = _windows_from_limits(payload) or _windows_from_named_keys(payload)
    windows.extend(_dollar_windows(payload))
    plan = payload.get("plan")
    return windows, str(plan) if plan else None, _notes_from_payload(payload)


# --------------------------------------------------------------------------------------
# Source 2: local transcript scrape (fallback only)
# --------------------------------------------------------------------------------------


def _iter_events(data_dir: Path) -> list[_Event]:
    events: list[_Event] = []
    if not data_dir.exists():
        return events

    for jsonl_path in data_dir.rglob("*.jsonl"):
        try:
            lines = jsonl_path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts = _parse_timestamp(record.get("timestamp", ""))
            usage = (record.get("message") or {}).get("usage")
            if ts is None or not usage:
                continue

            tokens = sum(
                usage.get(key, 0) or 0
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                )
            )
            events.append(_Event(timestamp=ts, tokens=tokens))

    events.sort(key=lambda e: e.timestamp)
    return events


def _current_block(events: list[_Event], window: timedelta) -> tuple[datetime | None, int]:
    """Return (block_start, tokens_in_block) for the active session block.

    A block starts at the first event and resets whenever two consecutive events are more
    than `window` apart -- same heuristic ccusage uses for Claude Code's rolling session limit.
    """
    if not events:
        return None, 0

    block_start = events[0].timestamp
    block_tokens = 0
    for event in events:
        if event.timestamp - block_start > window:
            block_start = event.timestamp
            block_tokens = 0
        block_tokens += event.tokens

    return block_start, block_tokens


def _trailing_sum(events: list[_Event], since: datetime) -> int:
    return sum(e.tokens for e in events if e.timestamp >= since)


# --------------------------------------------------------------------------------------
# Separate concern: API-key rate limit headers
# --------------------------------------------------------------------------------------


def _probe_live_api_limits() -> UsageWindow | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    body = json.dumps(
        {
            "model": "claude-haiku-4-5-20251001",
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode()
    request = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=body,
        method="POST",
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            headers = response.headers
    except urllib.error.HTTPError as exc:
        headers = exc.headers
    except (urllib.error.URLError, TimeoutError, OSError):
        return None

    limit = headers.get("anthropic-ratelimit-tokens-limit")
    remaining = headers.get("anthropic-ratelimit-tokens-remaining")
    reset = headers.get("anthropic-ratelimit-tokens-reset")
    if limit is None or remaining is None:
        return None

    return UsageWindow(
        label="API tokens/min (live key)",
        unit="tokens",
        used=float(limit) - float(remaining),
        limit=float(limit),
        reset_at=_parse_timestamp(reset) if reset else None,
        confidence=Confidence.AUTHORITATIVE,
        source="anthropic-ratelimit-tokens-* response headers",
        note="API-key rate limit, not subscription usage",
    )


class ClaudeProvider:
    name = "claude"

    def __init__(self, config: ClaudeConfig):
        self.config = config

    # -- source 1 ------------------------------------------------------------------
    def _fetch_live_usage(self) -> tuple[list[UsageWindow], str | None, list[str]]:
        """Raises CredentialError/UsageSourceError; both are caught by fetch()."""
        credential = load_claude_credential(self.config.credentials_file)
        if credential.is_expired:
            raise CredentialError(
                f"OAuth credential from {credential.source} expired "
                f"{credential.expires_at:%Y-%m-%d %H:%M UTC} -- run `claude` once to refresh"
            )

        windows, plan, notes = parse_oauth_usage(fetch_oauth_usage(credential))
        if not windows:
            raise UsageSourceError(
                "live usage response contained no recognizable windows "
                "(limits[]/five_hour/seven_day all absent) -- the payload shape may have changed"
            )
        return windows, plan or credential.subscription_type, notes

    # -- source 2 ------------------------------------------------------------------
    def _transcript_windows(self, now: datetime) -> tuple[list[UsageWindow], str | None]:
        events = _iter_events(self.config.data_dir)
        if not events:
            return [], (
                f"no Claude Code session transcripts found under {self.config.data_dir}. "
                "Run Claude Code at least once, or point CLAUDE_CONFIG_DIR at the right home."
            )

        window = timedelta(hours=self.config.session_window_hours)
        block_start, block_tokens = _current_block(events, window)
        session_limit = self.config.session_token_limit or None
        weekly_limit = self.config.weekly_token_limit or None

        windows = [
            UsageWindow(
                label=f"{self.config.session_window_hours:g}h session tokens (estimated)",
                unit="tokens",
                used=float(block_tokens),
                limit=float(session_limit) if session_limit else None,
                reset_at=block_start + window if block_start else None,
                confidence=Confidence.ESTIMATED if session_limit else Confidence.UNAVAILABLE,
                source=TRANSCRIPT_SOURCE,
                note=None
                if session_limit
                else "cap not readable locally; set claude.session_token_limit in config",
            ),
            UsageWindow(
                label="7d rolling tokens (estimated)",
                unit="tokens",
                used=float(_trailing_sum(events, now - timedelta(days=7))),
                limit=float(weekly_limit) if weekly_limit else None,
                reset_at=None,
                confidence=Confidence.ESTIMATED if weekly_limit else Confidence.UNAVAILABLE,
                source=TRANSCRIPT_SOURCE,
                note="weekly reset anchor is account-specific and not exposed locally",
            ),
        ]
        return windows, None

    def fetch(self) -> ProviderSnapshot:
        now = datetime.now(UTC)
        snapshot = ProviderSnapshot(provider=self.name, fetched_at=now)

        if self.config.use_oauth_usage_api:
            try:
                windows, plan, notes = self._fetch_live_usage()
            except (CredentialError, UsageSourceError) as exc:
                live_error = str(exc)
            else:
                snapshot.windows.extend(windows)
                snapshot.plan = plan
                snapshot.notes.extend(notes)
                live_error = None
        else:
            live_error = "live usage API disabled (claude.use_oauth_usage_api = false)"

        # Only degrade to the local estimate when the authoritative source gave us nothing.
        if not snapshot.windows:
            snapshot.errors.append(f"live usage API unavailable: {live_error}")
            transcript_windows, transcript_error = self._transcript_windows(now)
            if transcript_error:
                snapshot.errors.append(transcript_error)
            else:
                snapshot.errors.append("falling back to local transcript estimate")
                snapshot.windows.extend(transcript_windows)

        # Independent of the chain above: API-key rate limits measure a different thing.
        api_key_window = _probe_live_api_limits()
        if api_key_window:
            snapshot.windows.append(api_key_window)

        return snapshot
