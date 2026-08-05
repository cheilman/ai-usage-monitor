"""Claude usage provider.

Anthropic does not publish a "get my subscription usage" API. Two signals are combined, in
this order, and each one records a SourceAttempt whether it works or not (`ai-usage-monitor
doctor` prints those attempts -- both sources here read undocumented surfaces that will
eventually change, and an attempt list is what makes that visible instead of silent):

1. Live Anthropic API rate-limit headers (only if ANTHROPIC_API_KEY is set).
   Every API response carries anthropic-ratelimit-{requests,tokens}-{limit,remaining,reset}
   headers. These are authoritative but describe API-key rate limits, not the Claude Code
   subscription session window, so they're reported as a separate window. We use the free
   /v1/messages/count_tokens endpoint to trigger them without spending completion tokens.

2. Local transcript scraping (the subscription signal, for Claude Code Pro/Max/Team).
   Claude Code writes one JSONL file per session under ~/.claude/projects/<project>/*.jsonl,
   one JSON object per line, with token counts in `message.usage`. We sum tokens inside the
   active rolling session block (mirrors the community `ccusage` tool's approach: a new block
   starts whenever there's a gap >= session_window_hours between events) and inside the
   trailing 7 days. The Console/claude.ai plan cap itself is not published anywhere we can
   read programmatically, so `limit` comes from user config and defaults to None ("unavailable")
   -- this is the fallback this task's design notes asked to flag explicitly as fragile: it
   breaks silently if Anthropic changes the JSONL schema or storage location.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ai_usage_monitor.config import ClaudeConfig
from ai_usage_monitor.models import (
    Confidence,
    ProviderSnapshot,
    SourceOutcome,
    UsageWindow,
)
from ai_usage_monitor.providers.base import SourceResult, run_source

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages/count_tokens"
ANTHROPIC_VERSION = "2023-06-01"

LIVE_API_SOURCE = "anthropic live rate-limit headers"
TRANSCRIPTS_SOURCE = "claude code JSONL transcripts"


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


def _probe_live_api_limits() -> SourceResult:
    """Source 1: live API key rate-limit headers (authoritative, but API-key scoped)."""
    import os

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return (
            [],
            SourceOutcome.NO_CREDENTIAL,
            "ANTHROPIC_API_KEY is not set",
            "Export ANTHROPIC_API_KEY to see live API rate limits. Not needed for "
            "Claude Code subscription usage, which comes from local transcripts.",
        )

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
    status: int | None = None
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            headers = response.headers
            status = response.status
    except urllib.error.HTTPError as exc:
        # 4xx responses still carry rate-limit headers, so they're worth reading.
        headers = exc.headers
        status = exc.code
    except (urllib.error.URLError, TimeoutError) as exc:
        return (
            [],
            SourceOutcome.ERROR,
            f"could not reach {ANTHROPIC_API_URL}: {exc}",
            "Check network access and any proxy settings for api.anthropic.com.",
        )

    if status in (401, 403):
        return (
            [],
            SourceOutcome.ERROR,
            f"HTTP {status} from {ANTHROPIC_API_URL} -- key rejected",
            "ANTHROPIC_API_KEY looks invalid, expired, or revoked. Re-issue it at "
            "console.anthropic.com and re-export it.",
        )

    limit = headers.get("anthropic-ratelimit-tokens-limit")
    remaining = headers.get("anthropic-ratelimit-tokens-remaining")
    reset = headers.get("anthropic-ratelimit-tokens-reset")
    if limit is None or remaining is None:
        return (
            [],
            SourceOutcome.EMPTY,
            f"HTTP {status} but no anthropic-ratelimit-tokens-* headers on the response",
            "The endpoint answered without rate-limit headers; Anthropic may have "
            "changed or dropped them. This source needs updating.",
        )

    reset_at = _parse_timestamp(reset) if reset else None
    window = UsageWindow(
        label="API tokens/min (live key)",
        unit="tokens",
        used=float(limit) - float(remaining),
        limit=float(limit),
        reset_at=reset_at,
        confidence=Confidence.AUTHORITATIVE,
        source="anthropic-ratelimit-tokens-* response headers",
    )
    return [window], SourceOutcome.OK, f"HTTP {status}, tokens limit {limit}", None


def _read_transcripts(config: ClaudeConfig, now: datetime) -> SourceResult:
    """Source 2: local Claude Code JSONL transcripts (the subscription-usage signal)."""
    data_dir = config.data_dir
    if not data_dir.exists():
        return (
            [],
            SourceOutcome.NOT_FOUND,
            f"{data_dir} does not exist",
            "Run Claude Code at least once, or point CLAUDE_CONFIG_DIR at the home "
            "directory that holds your .claude/projects.",
        )

    transcript_count = sum(1 for _ in data_dir.rglob("*.jsonl"))
    events = _iter_events(data_dir)
    if not events:
        # Files but no parseable usage records is the interesting case: that's what schema
        # drift looks like from here, and it's indistinguishable from "never used" without
        # the file count.
        detail = (
            f"{transcript_count} transcript file(s) under {data_dir}, but no records with "
            "a timestamp and message.usage"
            if transcript_count
            else f"no *.jsonl transcripts under {data_dir}"
        )
        remediation = (
            "Transcripts exist but carry no usage data -- Claude Code's JSONL schema may "
            "have changed. This source needs updating."
            if transcript_count
            else "Run Claude Code at least once so it writes a session transcript."
        )
        return [], SourceOutcome.EMPTY, detail, remediation

    windows: list[UsageWindow] = []
    window = timedelta(hours=config.session_window_hours)
    block_start, block_tokens = _current_block(events, window)
    reset_at = block_start + window if block_start else None
    windows.append(
        UsageWindow(
            label=f"{config.session_window_hours:g}h session tokens",
            unit="tokens",
            used=float(block_tokens),
            limit=float(config.session_token_limit) if config.session_token_limit else None,
            reset_at=reset_at,
            confidence=Confidence.ESTIMATED
            if config.session_token_limit
            else Confidence.UNAVAILABLE,
            source="local JSONL transcript scrape (~/.claude/projects)",
            note=None
            if config.session_token_limit
            else "cap not published by Anthropic; set claude.session_token_limit in config",
        )
    )

    week_ago = now - timedelta(days=7)
    weekly_tokens = _trailing_sum(events, week_ago)
    windows.append(
        UsageWindow(
            label="7d rolling tokens",
            unit="tokens",
            used=float(weekly_tokens),
            limit=float(config.weekly_token_limit) if config.weekly_token_limit else None,
            reset_at=None,
            confidence=Confidence.ESTIMATED
            if config.weekly_token_limit
            else Confidence.UNAVAILABLE,
            source="local JSONL transcript scrape (~/.claude/projects)",
            note="weekly reset anchor is account-specific and not exposed locally",
        )
    )

    remediation = None
    if not (config.session_token_limit and config.weekly_token_limit):
        remediation = (
            "Usage is readable but plan caps are not published by Anthropic; set "
            "claude.session_token_limit / claude.weekly_token_limit in config for percentages."
        )
    detail = f"{len(events)} usage records across {transcript_count} transcript file(s)"
    return windows, SourceOutcome.OK, detail, remediation


class ClaudeProvider:
    name = "claude"

    def __init__(self, config: ClaudeConfig):
        self.config = config

    def fetch(self) -> ProviderSnapshot:
        now = datetime.now(UTC)
        snapshot = ProviderSnapshot(provider=self.name, fetched_at=now)

        # Ordered chain: live API first (authoritative where it applies), then local
        # transcripts. Both are tried every time -- they describe different limits, so a
        # hit on one doesn't make the other redundant.
        chain = [
            (LIVE_API_SOURCE, _probe_live_api_limits),
            (TRANSCRIPTS_SOURCE, lambda: _read_transcripts(self.config, now)),
        ]
        for source_name, source_fn in chain:
            windows, attempt = run_source(source_name, source_fn)
            snapshot.attempts.append(attempt)
            snapshot.windows.extend(windows)

        if not snapshot.windows:
            snapshot.errors.append(
                "No Claude usage available: every source failed. "
                "Run `ai-usage-monitor doctor --provider claude` for per-source detail."
            )

        return snapshot
