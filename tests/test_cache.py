import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_usage_monitor.cache import SnapshotCache, default_cache_path, fetch_snapshots
from ai_usage_monitor.models import Confidence, ProviderSnapshot, UsageWindow


class FakeProvider:
    """Counts fetches so tests can tell a cache hit from a live call."""

    def __init__(self, name="claude", used=1.0):
        self.name = name
        self.used = used
        self.calls = 0

    def fetch(self) -> ProviderSnapshot:
        self.calls += 1
        return ProviderSnapshot(
            provider=self.name,
            fetched_at=datetime.now(UTC),
            windows=[
                UsageWindow(
                    key="five_hour",
                    label="5h session tokens",
                    unit="tokens",
                    used=self.used * self.calls,
                    limit=100.0,
                    reset_at=datetime.now(UTC) + timedelta(hours=1),
                    confidence=Confidence.ESTIMATED,
                    source="fake",
                    note=None,
                )
            ],
            errors=["fake error"],
        )


@pytest.fixture
def cache(tmp_path) -> SnapshotCache:
    return SnapshotCache(tmp_path / "snapshot.json")


def test_default_cache_path_honours_env(monkeypatch, tmp_path):
    monkeypatch.delenv("AI_USAGE_MONITOR_CACHE", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert default_cache_path() == tmp_path / "ai-usage-monitor" / "snapshot.json"

    monkeypatch.setenv("AI_USAGE_MONITOR_CACHE", str(tmp_path / "override.json"))
    assert default_cache_path() == tmp_path / "override.json"


def test_round_trip_preserves_snapshot(cache):
    original = FakeProvider().fetch()
    cache.put(original)
    restored = cache.get("claude", max_age=60)

    assert restored == original


def test_ttl_hit_then_miss(cache):
    now = datetime.now(UTC)
    cache.put(FakeProvider().fetch(), now=now)

    assert cache.get("claude", max_age=60, now=now + timedelta(seconds=59)) is not None
    assert cache.get("claude", max_age=60, now=now + timedelta(seconds=61)) is None


def test_zero_max_age_is_always_a_miss(cache):
    cache.put(FakeProvider().fetch())
    assert cache.get("claude", max_age=0) is None


def test_entry_from_the_future_is_a_miss(cache):
    """Clock skew shouldn't pin us to a stale entry forever."""
    now = datetime.now(UTC)
    cache.put(FakeProvider().fetch(), now=now + timedelta(hours=1))
    assert cache.get("claude", max_age=60, now=now) is None


def test_providers_do_not_clobber_each_other(cache):
    cache.put(FakeProvider(name="claude").fetch())
    cache.put(FakeProvider(name="gemini").fetch())

    assert cache.get("claude", max_age=60) is not None
    assert cache.get("gemini", max_age=60) is not None
    assert cache.get("codex", max_age=60) is None


def test_fetch_snapshots_serves_second_call_from_cache(cache):
    provider = FakeProvider()
    now = datetime.now(UTC)

    first = fetch_snapshots([provider], cache=cache, max_age=60, now=now)
    second = fetch_snapshots([provider], cache=cache, max_age=60, now=now + timedelta(seconds=30))

    assert provider.calls == 1
    assert second == first

    third = fetch_snapshots([provider], cache=cache, max_age=60, now=now + timedelta(seconds=61))
    assert provider.calls == 2
    assert third != first


def test_fetch_snapshots_without_cache_never_reads_or_writes(cache):
    """`--no-cache` passes cache=None: no read of a warm entry, no write of a fresh one."""
    provider = FakeProvider()
    cache.put(provider.fetch(), now=datetime.now(UTC))
    before = cache.path.read_text()

    fetch_snapshots([provider], cache=None, max_age=60)
    fetch_snapshots([provider], cache=None, max_age=60)

    assert provider.calls == 3  # 1 to seed the cache + 2 live fetches
    assert cache.path.read_text() == before


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json at all",
        '{"version": 1, "entries": {"claude": ',  # truncated mid-write
        '{"version": 999, "entries": {"claude": {"cached_at": "2026-01-01T00:00:00+00:00"}}}',
        '{"version": 1, "entries": "not-a-dict"}',
        '{"version": 1, "entries": {"claude": {"cached_at": "nonsense", "snapshot": {}}}}',
        '{"version": 1, "entries": {"claude": {"snapshot": {"provider": "claude"}}}}',
        '[1, 2, 3]',
    ],
)
def test_corrupt_cache_degrades_to_live_fetch(tmp_path, content):
    path = tmp_path / "snapshot.json"
    path.write_text(content)
    cache = SnapshotCache(path)
    provider = FakeProvider()

    snapshots = fetch_snapshots([provider], cache=cache, max_age=60)

    assert provider.calls == 1
    assert [s.provider for s in snapshots] == ["claude"]
    # ...and the bad file is replaced by a good one, so it self-heals.
    assert cache.get("claude", max_age=60) is not None


def test_snapshot_missing_fetched_at_is_a_miss(tmp_path):
    """A structurally valid but semantically broken entry must not crash the read."""
    path = tmp_path / "snapshot.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    "claude": {
                        "cached_at": datetime.now(UTC).isoformat(),
                        "snapshot": {"provider": "claude", "windows": [], "errors": []},
                    }
                },
            }
        )
    )
    assert SnapshotCache(path).get("claude", max_age=60) is None


def test_unwritable_cache_dir_does_not_raise(tmp_path):
    """A cache we can't write is a missing cache, not a crash."""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    cache = SnapshotCache(blocker / "snapshot.json")
    provider = FakeProvider()

    snapshots = fetch_snapshots([provider], cache=cache, max_age=60)

    assert provider.calls == 1
    assert len(snapshots) == 1


def test_cache_file_contains_no_credentials(monkeypatch, tmp_path):
    """The snapshot model has no token fields, so nothing secret can reach disk -- assert it."""
    from ai_usage_monitor.config import ClaudeConfig
    from ai_usage_monitor.providers import claude as claude_module

    secret = "sk-ant-oat01-DO-NOT-CACHE-ME"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    # Stand in for the live probe so the test never touches the network, while still
    # exercising the path where an authenticated call contributed a window.
    monkeypatch.setattr(
        claude_module,
        "_probe_live_api_limits",
        lambda: UsageWindow(
            key="api_tokens_per_minute",
            label="API tokens/min (live key)",
            unit="tokens",
            used=10.0,
            limit=100.0,
            reset_at=None,
            confidence=Confidence.AUTHORITATIVE,
            source="anthropic-ratelimit-tokens-* response headers",
            note=None,
        ),
    )

    provider = claude_module.ClaudeProvider(ClaudeConfig(data_dir=tmp_path / "no-such-dir"))
    cache = SnapshotCache(tmp_path / "snapshot.json")
    cache.put(provider.fetch())

    written = cache.path.read_text()
    assert secret not in written
    assert "ANTHROPIC_API_KEY" not in written

    # Belt and braces: the serialized schema is a closed set of known-safe keys.
    entry = json.loads(written)["entries"]["claude"]
    assert set(entry) == {"cached_at", "snapshot"}
    assert set(entry["snapshot"]) == {
        "provider",
        "status",
        "fetched_at",
        "plan",
        "windows",
        "attempts",
        "notes",
        "errors",
    }
    for window in entry["snapshot"]["windows"]:
        assert set(window) == {
            "key",
            "label",
            "unit",
            "used",
            "limit",
            "percent",
            "reset_at",
            "confidence",
            "source",
            "note",
            "is_active",
            "severity",
        }


# Both sources pointed at nothing, so the run is hermetic: no keychain, no network, no
# dependence on whether the developer's own machine happens to have usage data lying around
# (mirrors tests/test_cli.py's OFFLINE_CONFIG, so a live 429 or a stale local login can't
# make the exit code -- and therefore these cache-behavior assertions -- flaky).
_OFFLINE_CONFIG = """\
[claude]
use_oauth_usage_api = false

[gemini]
telemetry_log = "{telemetry_log}"
"""


def _run_cli(args, cache_path: Path, tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text(_OFFLINE_CONFIG.format(telemetry_log=tmp_path / "no-telemetry.log"))
    env = {
        **os.environ,
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-home"),
        "AI_USAGE_MONITOR_CONFIG": str(config),
        "CLAUDE_CREDENTIALS_FILE": str(tmp_path / "no-credentials.json"),
        "AI_USAGE_MONITOR_CACHE": str(cache_path),
    }
    env.pop("ANTHROPIC_API_KEY", None)  # keep the CLI offline
    return subprocess.run(
        [sys.executable, "-m", "ai_usage_monitor", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_cli_snapshot_writes_then_reads_cache(tmp_path):
    cache_path = tmp_path / "cache" / "snapshot.json"

    first = _run_cli(["snapshot", "--json"], cache_path, tmp_path)
    assert first.returncode == 2, first.stderr
    assert cache_path.exists()

    second = _run_cli(["snapshot", "--json"], cache_path, tmp_path)
    assert second.returncode == 2, second.stderr
    # Same fetched_at across two processes => the second one served from cache.
    # (`generated_at` is stamped fresh on every call, so compare providers, not the envelope.)
    assert json.loads(second.stdout)["providers"] == json.loads(first.stdout)["providers"]


def test_cli_no_cache_bypasses_a_warm_entry(tmp_path):
    cache_path = tmp_path / "snapshot.json"
    cache = SnapshotCache(cache_path)
    stale = FakeProvider().fetch()
    stale.windows[0].label = "STALE-SENTINEL"
    cache.put(stale)
    before = cache_path.read_text()

    result = _run_cli(["snapshot", "--json", "--no-cache"], cache_path, tmp_path)

    assert result.returncode == 2, result.stderr
    assert "STALE-SENTINEL" not in result.stdout
    assert cache_path.read_text() == before  # --no-cache doesn't write either


def test_cli_max_age_zero_forces_live_fetch(tmp_path):
    cache_path = tmp_path / "snapshot.json"
    cache = SnapshotCache(cache_path)
    stale = FakeProvider().fetch()
    stale.windows[0].label = "STALE-SENTINEL"
    cache.put(stale)

    result = _run_cli(["snapshot", "--json", "--max-age", "0"], cache_path, tmp_path)

    assert result.returncode == 2, result.stderr
    assert "STALE-SENTINEL" not in result.stdout
    # Unlike --no-cache, a live fetch here still refreshes the cache for the next reader.
    assert "STALE-SENTINEL" not in cache_path.read_text()


def test_cli_survives_a_corrupt_cache_file(tmp_path):
    cache_path = tmp_path / "snapshot.json"
    cache_path.write_text("{{{ not json")

    result = _run_cli(["snapshot", "--json"], cache_path, tmp_path)

    assert result.returncode == 2, result.stderr
    providers = json.loads(result.stdout)["providers"]
    assert {entry["provider"] for entry in providers} == {"claude", "gemini"}
