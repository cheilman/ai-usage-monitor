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
we have a number.

Request caps (e.g. free-tier daily request limits) are not discoverable at all locally, so
those come from `daily_request_limit` in config, sourced from gemini-cli's published quota
docs and known to go stale whenever Google changes tier limits.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ai_usage_monitor.config import GeminiConfig
from ai_usage_monitor.models import Confidence, ProviderSnapshot, UsageWindow

API_RESPONSE_EVENT = "gemini_cli.api_response"


@dataclass
class _Event:
    timestamp: datetime
    tokens: int


def _parse_timestamp(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, AttributeError):
        return None


def _iter_events(log_path: Path) -> list[_Event]:
    events: list[_Event] = []
    if not log_path.exists():
        return events

    try:
        lines = log_path.read_text(errors="replace").splitlines()
    except OSError:
        return events

    for line in lines:
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


class GeminiProvider:
    name = "gemini"

    def __init__(self, config: GeminiConfig):
        self.config = config

    def fetch(self) -> ProviderSnapshot:
        now = datetime.now(UTC)
        snapshot = ProviderSnapshot(provider=self.name, fetched_at=now)

        events = _iter_events(self.config.telemetry_log)
        if not events:
            snapshot.errors.append(
                f"No Gemini CLI telemetry found at {self.config.telemetry_log}. Enable local "
                'telemetry in ~/.gemini/settings.json: {"telemetry": {"enabled": true, '
                '"target": "local", "outfile": "<path>"}}. Until then Gemini usage is unavailable.'
            )
            snapshot.windows.append(
                UsageWindow(
                    label="daily requests",
                    unit="requests",
                    used=None,
                    limit=float(self.config.daily_request_limit)
                    if self.config.daily_request_limit
                    else None,
                    reset_at=None,
                    confidence=Confidence.UNAVAILABLE,
                    source="gemini-cli local telemetry log (not found)",
                )
            )
            return snapshot

        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        todays_events = [e for e in events if e.timestamp >= day_start]
        next_midnight = day_start + timedelta(days=1)

        snapshot.windows.append(
            UsageWindow(
                label="today's requests",
                unit="requests",
                used=float(len(todays_events)),
                limit=float(self.config.daily_request_limit)
                if self.config.daily_request_limit
                else None,
                reset_at=next_midnight,
                confidence=Confidence.ESTIMATED,
                source="gemini-cli local telemetry log",
                note="request cap sourced from published quota docs, not a live API",
            )
        )

        snapshot.windows.append(
            UsageWindow(
                label="today's tokens",
                unit="tokens",
                used=float(sum(e.tokens for e in todays_events)),
                limit=None,
                reset_at=next_midnight,
                confidence=Confidence.ESTIMATED,
                source="gemini-cli local telemetry log",
                note="Google does not publish a token cap for this tier",
            )
        )

        return snapshot
