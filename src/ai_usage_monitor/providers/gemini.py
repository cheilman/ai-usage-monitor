"""Gemini usage provider.

Google publishes no "get my usage" API for Gemini CLI / AI Studio consumer plans either.
The CLI's own `/stats model` command shows session tokens and quota interactively, but that
output isn't reachable non-interactively or scriptably.

The only local, file-based signal is gemini-cli's OpenTelemetry log, which is OFF by default.
A user has to opt in via ~/.gemini/settings.json:

    {"telemetry": {"enabled": true, "target": "local", "outfile": "~/.gemini/telemetry.log"}}

Once enabled, each `gemini_cli.api_response` event line carries input/output token counts we
can sum over a trailing window. This is explicitly fragile: it depends on the user having
opted in, on Google not changing the event schema, and on the outfile path staying put -- if
the file is missing we report Confidence.UNAVAILABLE with instructions rather than pretending
we have a number. Every attempt to read it -- missing, present-but-empty, schema drift, or
success -- records a SourceAttempt, which is what `ai-usage-monitor doctor` prints.

Request caps (e.g. free-tier daily request limits) are not discoverable at all locally, so
those come from `daily_request_limit` in config, sourced from gemini-cli's published quota
docs and known to go stale whenever Google changes tier limits.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ai_usage_monitor.config import GeminiConfig
from ai_usage_monitor.models import (
    Confidence,
    ProviderSnapshot,
    SourceOutcome,
    UsageWindow,
)
from ai_usage_monitor.providers.base import SourceResult, run_source

API_RESPONSE_EVENT = "gemini_cli.api_response"

TELEMETRY_SOURCE = "gemini-cli local telemetry log"

ENABLE_TELEMETRY_HINT = (
    'Enable local telemetry in ~/.gemini/settings.json: {"telemetry": {"enabled": true, '
    '"target": "local", "outfile": "<path>"}}, then restart gemini-cli.'
)


@dataclass
class _Event:
    timestamp: datetime
    tokens: int


def _parse_timestamp(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, AttributeError):
        return None


def _parse_events(text: str) -> list[_Event]:
    events: list[_Event] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        name = record.get("name") or record.get("event") or ""
        if API_RESPONSE_EVENT not in name:
            continue

        attrs = record.get("attributes", record)
        ts = _parse_timestamp(record.get("timestamp", "") or attrs.get("timestamp", ""))
        if ts is None:
            continue

        tokens = (attrs.get("input_token_count", 0) or 0) + (
            attrs.get("output_token_count", 0) or 0
        )
        events.append(_Event(timestamp=ts, tokens=tokens))

    events.sort(key=lambda e: e.timestamp)
    return events


def _placeholder_window(config: GeminiConfig) -> UsageWindow:
    """Shown when telemetry gives us nothing, so the cap is still visible next to a '-'."""
    return UsageWindow(
        label="daily requests",
        unit="requests",
        used=None,
        limit=float(config.daily_request_limit) if config.daily_request_limit else None,
        reset_at=None,
        confidence=Confidence.UNAVAILABLE,
        source="gemini-cli local telemetry log (no data)",
    )


def _read_telemetry(config: GeminiConfig, now: datetime) -> SourceResult:
    """Only source: gemini-cli's opt-in OpenTelemetry log."""
    log_path = config.telemetry_log
    if not log_path.exists():
        return (
            [_placeholder_window(config)],
            SourceOutcome.NOT_FOUND,
            f"{log_path} does not exist",
            ENABLE_TELEMETRY_HINT,
        )

    try:
        text = log_path.read_text(errors="replace")
    except OSError as exc:
        return (
            [_placeholder_window(config)],
            SourceOutcome.ERROR,
            f"could not read {log_path}: {exc}",
            "Check permissions on the telemetry log, or point gemini.telemetry_log at "
            "the file gemini-cli is actually writing.",
        )

    events = _parse_events(text)
    if not events:
        line_count = len(text.splitlines())
        return (
            [_placeholder_window(config)],
            SourceOutcome.EMPTY,
            f"{log_path} has {line_count} line(s) but no usable "
            f"{API_RESPONSE_EVENT} events",
            ENABLE_TELEMETRY_HINT
            if not line_count
            else f"The log has content but no {API_RESPONSE_EVENT} events with token "
            "counts -- gemini-cli's telemetry schema may have changed.",
        )

    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    todays_events = [e for e in events if e.timestamp >= day_start]
    next_midnight = day_start + timedelta(days=1)

    windows = [
        UsageWindow(
            label="today's requests",
            unit="requests",
            used=float(len(todays_events)),
            limit=float(config.daily_request_limit) if config.daily_request_limit else None,
            reset_at=next_midnight,
            confidence=Confidence.ESTIMATED,
            source=TELEMETRY_SOURCE,
            note="request cap sourced from published quota docs, not a live API",
        ),
        UsageWindow(
            label="today's tokens",
            unit="tokens",
            used=float(sum(e.tokens for e in todays_events)),
            limit=None,
            reset_at=next_midnight,
            confidence=Confidence.ESTIMATED,
            source=TELEMETRY_SOURCE,
            note="Google does not publish a token cap for this tier",
        ),
    ]

    remediation = (
        None
        if config.daily_request_limit
        else "Set gemini.daily_request_limit in config to get a percentage instead of '?'."
    )
    detail = f"{len(events)} event(s) in log, {len(todays_events)} today"
    return windows, SourceOutcome.OK, detail, remediation


class GeminiProvider:
    name = "gemini"

    def __init__(self, config: GeminiConfig):
        self.config = config

    def fetch(self) -> ProviderSnapshot:
        now = datetime.now(UTC)
        snapshot = ProviderSnapshot(provider=self.name, fetched_at=now)

        windows, attempt = run_source(TELEMETRY_SOURCE, lambda: _read_telemetry(self.config, now))
        snapshot.attempts.append(attempt)
        snapshot.windows.extend(windows)

        if not attempt.ok:
            snapshot.errors.append(
                f"No Gemini usage available: {attempt.detail}. "
                "Run `ai-usage-monitor doctor --provider gemini` for remediation steps."
            )

        return snapshot
