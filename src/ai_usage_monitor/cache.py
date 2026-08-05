"""On-disk snapshot cache, so a long-running dashboard doesn't hammer the providers.

The dashboard refreshes on a timer and the Claude provider leans on endpoints/formats we do
not own. The numbers we report (a rolling 5-hour window, a 7-day window, a daily request
count) move slowly, so serving a snapshot up to `max_age` seconds old costs essentially
nothing in accuracy while cutting request volume by whatever ratio the user picks.

Layout of ~/.cache/ai-usage-monitor/snapshot.json:

    {
      "version": 1,
      "entries": {
        "claude": {"cached_at": "2026-08-05T12:00:00+00:00", "snapshot": {...}}
      }
    }

Entries are keyed by provider, so `--provider claude` never clobbers gemini's cached data.
The stored payload is exactly what `snapshot_to_dict` emits -- labels, counts, timestamps.
API keys and OAuth tokens are not part of `ProviderSnapshot` at all, so they cannot end up
in this file (`tests/test_cache.py` asserts that, rather than trusting the reasoning).

Every failure mode here is a cache *miss*, never an error: an unreadable, truncated, corrupt,
or future-versioned file just means we fetch live. A cache that can break the tool is worse
than no cache.
"""

from __future__ import annotations

import json
import os
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from ai_usage_monitor.models import ProviderSnapshot
from ai_usage_monitor.providers.base import UsageProvider
from ai_usage_monitor.render import snapshot_from_dict, snapshot_to_dict

CACHE_VERSION = 1
DEFAULT_MAX_AGE = 60.0


def default_cache_path() -> Path:
    """$AI_USAGE_MONITOR_CACHE, else $XDG_CACHE_HOME/..., else ~/.cache/..."""
    override = os.environ.get("AI_USAGE_MONITOR_CACHE")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    root = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return root / "ai-usage-monitor" / "snapshot.json"


class SnapshotCache:
    """Per-provider snapshot store with a TTL supplied per read."""

    def __init__(self, path: Path | None = None):
        self.path = path or default_cache_path()

    def _load_entries(self) -> dict:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError, UnicodeDecodeError):
            return {}
        if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
            return {}
        entries = raw.get("entries")
        return entries if isinstance(entries, dict) else {}

    def get(
        self,
        provider: str,
        max_age: float = DEFAULT_MAX_AGE,
        now: datetime | None = None,
    ) -> ProviderSnapshot | None:
        """Return the cached snapshot if it's younger than `max_age` seconds, else None."""
        if max_age <= 0:
            return None

        entry = self._load_entries().get(provider)
        if not isinstance(entry, dict):
            return None

        now = now or datetime.now(UTC)
        try:
            cached_at = datetime.fromisoformat(entry["cached_at"])
            age = (now - cached_at).total_seconds()
        except (KeyError, TypeError, ValueError):
            return None

        # A negative age means the clock moved backwards (or someone hand-edited the file);
        # treat it as a miss rather than trusting an entry from the "future" indefinitely.
        if age < 0 or age > max_age:
            return None

        try:
            return snapshot_from_dict(entry["snapshot"])
        except (KeyError, TypeError, ValueError):
            return None

    def put(self, snapshot: ProviderSnapshot, now: datetime | None = None) -> None:
        """Store one provider's snapshot, leaving other providers' entries intact.

        Best effort: a read-only cache dir or a full disk must not break the CLI.
        """
        now = now or datetime.now(UTC)
        entries = self._load_entries()
        entries[snapshot.provider] = {
            "cached_at": now.isoformat(),
            "snapshot": snapshot_to_dict(snapshot),
        }
        payload = json.dumps({"version": CACHE_VERSION, "entries": entries}, indent=2)

        tmp = self.path.parent / f".{self.path.name}.{os.getpid()}.tmp"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload)
            # Written atomically so a concurrent dashboard never reads a half-written file.
            os.replace(tmp, self.path)
        except OSError:
            with suppress(OSError):
                tmp.unlink(missing_ok=True)


def fetch_snapshots(
    providers: list[UsageProvider],
    cache: SnapshotCache | None = None,
    max_age: float = DEFAULT_MAX_AGE,
    now: datetime | None = None,
) -> list[ProviderSnapshot]:
    """Fetch each provider, reading through `cache` when one is given (None = --no-cache).

    Both `snapshot` and `dashboard` go through here so they cannot drift apart on caching
    behaviour the way they could if each did its own `p.fetch()`.
    """
    snapshots = []
    for provider in providers:
        cached = cache.get(provider.name, max_age=max_age, now=now) if cache else None
        if cached is not None:
            snapshots.append(cached)
            continue

        snapshot = provider.fetch()
        if cache is not None:
            cache.put(snapshot, now=now)
        snapshots.append(snapshot)
    return snapshots
