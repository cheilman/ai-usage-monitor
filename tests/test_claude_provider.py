import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ai_usage_monitor.config import ClaudeConfig
from ai_usage_monitor.models import Confidence, SourceOutcome
from ai_usage_monitor.providers.claude import (
    LIVE_API_SOURCE,
    TRANSCRIPTS_SOURCE,
    ClaudeProvider,
)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records))


def test_no_data_dir_reports_unavailable(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    provider = ClaudeProvider(ClaudeConfig(data_dir=tmp_path / "missing"))
    snapshot = provider.fetch()
    assert snapshot.provider == "claude"
    assert snapshot.errors
    assert snapshot.windows == []


def test_all_sources_failing_still_yields_full_attempt_list(tmp_path: Path, monkeypatch):
    """Every source failing must produce a complete audit trail, not an empty/crashed result."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    provider = ClaudeProvider(ClaudeConfig(data_dir=tmp_path / "missing"))
    snapshot = provider.fetch()

    assert [a.name for a in snapshot.attempts] == [LIVE_API_SOURCE, TRANSCRIPTS_SOURCE]
    assert not any(a.ok for a in snapshot.attempts)
    assert snapshot.status == Confidence.UNAVAILABLE

    live, transcripts = snapshot.attempts
    assert live.outcome == SourceOutcome.NO_CREDENTIAL
    assert "ANTHROPIC_API_KEY" in live.detail
    assert transcripts.outcome == SourceOutcome.NOT_FOUND
    assert "missing" in transcripts.detail

    # Every failed attempt has to tell the user what to actually do about it.
    assert all(a.remediation for a in snapshot.attempts)
    assert all(a.duration_ms >= 0 for a in snapshot.attempts)


def test_transcripts_present_but_unparseable_is_empty_not_not_found(tmp_path: Path, monkeypatch):
    """Schema drift (files exist, no usage records) must be distinguishable from 'never used'."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    data_dir = tmp_path / "projects" / "p"
    data_dir.mkdir(parents=True)
    _write_jsonl(data_dir / "session.jsonl", [{"timestamp": "2026-01-01T00:00:00+00:00"}])

    provider = ClaudeProvider(ClaudeConfig(data_dir=tmp_path / "projects"))
    attempt = next(a for a in provider.fetch().attempts if a.name == TRANSCRIPTS_SOURCE)

    assert attempt.outcome == SourceOutcome.EMPTY
    assert "1 transcript file(s)" in attempt.detail
    assert "schema may have changed" in attempt.remediation


def test_successful_transcript_source_records_ok_attempt(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    data_dir = tmp_path / "projects" / "p"
    data_dir.mkdir(parents=True)
    _write_jsonl(
        data_dir / "session.jsonl",
        [
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "message": {"usage": {"input_tokens": 10, "output_tokens": 5}},
            }
        ],
    )

    config = ClaudeConfig(
        data_dir=tmp_path / "projects", session_token_limit=100, weekly_token_limit=200
    )
    attempt = next(a for a in ClaudeProvider(config).fetch().attempts if a.name == TRANSCRIPTS_SOURCE)

    assert attempt.outcome == SourceOutcome.OK
    assert attempt.ok
    assert "1 usage records" in attempt.detail
    assert attempt.remediation is None


def test_source_exception_becomes_error_attempt(tmp_path: Path, monkeypatch):
    """A source that blows up degrades to an `error` attempt instead of killing the snapshot."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        "ai_usage_monitor.providers.claude._read_transcripts",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    snapshot = ClaudeProvider(ClaudeConfig(data_dir=tmp_path)).fetch()
    attempt = next(a for a in snapshot.attempts if a.name == TRANSCRIPTS_SOURCE)

    assert attempt.outcome == SourceOutcome.ERROR
    assert "RuntimeError: boom" in attempt.detail
    assert snapshot.status == Confidence.UNAVAILABLE


def test_sums_tokens_in_active_block(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    data_dir = tmp_path / "projects" / "my-project"
    data_dir.mkdir(parents=True)

    now = datetime.now(UTC)
    recent = now - timedelta(minutes=10)
    old = now - timedelta(hours=10)  # outside the 5h window -> separate block

    _write_jsonl(
        data_dir / "session.jsonl",
        [
            {
                "timestamp": old.isoformat(),
                "message": {"usage": {"input_tokens": 500, "output_tokens": 500}},
            },
            {
                "timestamp": recent.isoformat(),
                "message": {"usage": {"input_tokens": 100, "output_tokens": 50}},
            },
        ],
    )

    provider = ClaudeProvider(ClaudeConfig(data_dir=tmp_path / "projects", session_token_limit=1000))
    snapshot = provider.fetch()

    session_window = next(w for w in snapshot.windows if "session" in w.label)
    assert session_window.used == 150
    assert session_window.limit == 1000
    assert session_window.confidence == Confidence.ESTIMATED

    weekly_window = next(w for w in snapshot.windows if "7d" in w.label)
    assert weekly_window.used == 1150


def test_malformed_lines_are_skipped(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    data_dir = tmp_path / "projects" / "p"
    data_dir.mkdir(parents=True)
    (data_dir / "session.jsonl").write_text("not json\n{}\n")

    provider = ClaudeProvider(ClaudeConfig(data_dir=tmp_path / "projects"))
    snapshot = provider.fetch()
    assert snapshot.errors
