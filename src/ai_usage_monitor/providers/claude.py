"""Claude usage provider.

Anthropic does not publish a "get my subscription usage" API. Two signals are combined:

1. Local transcript scraping (primary, works for Claude Code Pro/Max/Team subscriptions).
   Claude Code writes one JSONL file per session under ~/.claude/projects/<project>/*.jsonl,
   one JSON object per line, with token counts in `message.usage`. We sum tokens inside the
   active rolling session block (mirrors the community `ccusage` tool's approach: a new block
   starts whenever there's a gap >= session_window_hours between events) and inside the
   trailing 7 days. The Console/claude.ai plan cap itself is not published anywhere we can
   read programmatically, so `limit` comes from user config and defaults to None ("unavailable")
   -- this is the fallback this task's design notes asked to flag explicitly as fragile: it
   breaks silently if Anthropic changes the JSONL schema or storage location.

2. Live Anthropic API rate-limit headers (secondary, only if ANTHROPIC_API_KEY is set).
   Every API response carries anthropic-ratelimit-{requests,tokens}-{limit,remaining,reset}
   headers. These are authoritative but describe API-key rate limits, not the Claude Code
   subscription session window, so they're reported as a separate window. We use the free
   /v1/messages/count_tokens endpoint to trigger them without spending completion tokens.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ai_usage_monitor.config import ClaudeConfig
from ai_usage_monitor.models import Confidence, ProviderSnapshot, UsageWindow

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages/count_tokens"
ANTHROPIC_VERSION = "2023-06-01"


@dataclass
class _Event:
    timestamp: datetime
    tokens: int


def _parse_timestamp(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, AttributeError):
        return None


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


def _probe_live_api_limits(now: datetime) -> UsageWindow | None:
    import os

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
    except (urllib.error.URLError, TimeoutError):
        return None

    limit = headers.get("anthropic-ratelimit-tokens-limit")
    remaining = headers.get("anthropic-ratelimit-tokens-remaining")
    reset = headers.get("anthropic-ratelimit-tokens-reset")
    if limit is None or remaining is None:
        return None

    reset_at = _parse_timestamp(reset) if reset else None
    return UsageWindow(
        label="API tokens/min (live key)",
        unit="tokens",
        used=float(limit) - float(remaining),
        limit=float(limit),
        reset_at=reset_at,
        confidence=Confidence.AUTHORITATIVE,
        source="anthropic-ratelimit-tokens-* response headers",
    )


class ClaudeProvider:
    name = "claude"

    def __init__(self, config: ClaudeConfig):
        self.config = config

    def fetch(self) -> ProviderSnapshot:
        now = datetime.now(UTC)
        snapshot = ProviderSnapshot(provider=self.name, fetched_at=now)

        events = _iter_events(self.config.data_dir)
        if not events:
            snapshot.errors.append(
                f"No Claude Code session transcripts found under {self.config.data_dir}. "
                "Run Claude Code at least once, or point CLAUDE_CONFIG_DIR at the right home."
            )
        else:
            window = timedelta(hours=self.config.session_window_hours)
            block_start, block_tokens = _current_block(events, window)
            reset_at = block_start + window if block_start else None
            snapshot.windows.append(
                UsageWindow(
                    label=f"{self.config.session_window_hours:g}h session tokens",
                    unit="tokens",
                    used=float(block_tokens),
                    limit=float(self.config.session_token_limit)
                    if self.config.session_token_limit
                    else None,
                    reset_at=reset_at,
                    confidence=Confidence.ESTIMATED
                    if self.config.session_token_limit
                    else Confidence.UNAVAILABLE,
                    source="local JSONL transcript scrape (~/.claude/projects)",
                    note=None
                    if self.config.session_token_limit
                    else "cap not published by Anthropic; set claude.session_token_limit in config",
                )
            )

            week_ago = now - timedelta(days=7)
            weekly_tokens = _trailing_sum(events, week_ago)
            snapshot.windows.append(
                UsageWindow(
                    label="7d rolling tokens",
                    unit="tokens",
                    used=float(weekly_tokens),
                    limit=float(self.config.weekly_token_limit)
                    if self.config.weekly_token_limit
                    else None,
                    reset_at=None,
                    confidence=Confidence.ESTIMATED
                    if self.config.weekly_token_limit
                    else Confidence.UNAVAILABLE,
                    source="local JSONL transcript scrape (~/.claude/projects)",
                    note="weekly reset anchor is account-specific and not exposed locally",
                )
            )

        live_window = _probe_live_api_limits(now)
        if live_window:
            snapshot.windows.append(live_window)

        return snapshot
