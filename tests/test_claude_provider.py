import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_usage_monitor.config import ClaudeConfig
from ai_usage_monitor.credentials import CredentialError, OAuthCredential
from ai_usage_monitor.models import Confidence, ProviderStatus, SourceOutcome
from ai_usage_monitor.providers import claude as claude_module
from ai_usage_monitor.providers.claude import (
    OAUTH_SOURCE,
    TRANSCRIPT_SOURCE,
    ClaudeProvider,
    UsageSourceError,
    parse_oauth_usage,
)

FIXTURE = Path(__file__).parent / "fixtures" / "claude_oauth_usage.json"


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records))


def _seed_transcripts(tmp_path: Path) -> Path:
    """Write a transcript the fallback would happily read, and return its projects dir."""
    data_dir = tmp_path / "projects" / "my-project"
    data_dir.mkdir(parents=True)
    _write_jsonl(
        data_dir / "session.jsonl",
        [
            {
                "timestamp": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
                "message": {"usage": {"input_tokens": 100, "output_tokens": 50}},
            }
        ],
    )
    return tmp_path / "projects"


def _credential(expires_at: datetime | None = None) -> OAuthCredential:
    return OAuthCredential(
        access_token="fake-token",
        expires_at=expires_at or datetime.now(UTC) + timedelta(hours=1),
        subscription_type="pro",
        source="test",
    )


def _no_credential(path):
    raise CredentialError("no Claude Code OAuth credential found (looked in keychain)")


# ---------------------------------------------------------------------------------------
# Golden fixture: the live /api/oauth/usage payload shape
# ---------------------------------------------------------------------------------------


def test_golden_fixture_parses_to_authoritative_windows():
    payload = json.loads(FIXTURE.read_text())
    windows, plan, notes = parse_oauth_usage(payload)

    assert plan == "pro"
    assert notes == []  # extra usage off, zero spend, no limit
    assert [w.label for w in windows] == ["5-hour session", "Weekly (all models)"]

    session, weekly = windows
    assert (session.used, session.limit, session.unit) == (21.0, 100.0, "percent")
    assert session.percent == 21.0
    assert session.severity == "normal"
    assert session.is_active is False
    assert session.reset_at == datetime.fromisoformat("2026-08-02T07:20:00.628697+00:00")
    assert session.confidence == Confidence.AUTHORITATIVE
    assert session.source == OAUTH_SOURCE

    assert weekly.percent == 54.0
    assert weekly.is_active is True

    # No user-supplied cap was involved anywhere in this path.
    assert all(w.note is None for w in windows)


def test_prefers_limits_array_over_named_keys():
    payload = {
        "five_hour": {"utilization": 99.0, "resets_at": None},
        "limits": [{"kind": "session", "percent": 21, "severity": "normal", "is_active": True}],
    }
    windows, _, _ = parse_oauth_usage(payload)
    assert [(w.label, w.used) for w in windows] == [("5-hour session", 21.0)]


def test_falls_back_to_named_keys_when_limits_empty():
    payload = {
        "limits": [],
        "five_hour": {"utilization": 12.5, "resets_at": "2026-08-02T07:20:00+00:00"},
        "seven_day": {"utilization": 40.0, "resets_at": "2026-08-03T00:00:00+00:00"},
        "seven_day_opus": None,
    }
    windows, _, _ = parse_oauth_usage(payload)
    assert [(w.label, w.used) for w in windows] == [
        ("5-hour session", 12.5),
        ("Weekly (all models)", 40.0),
    ]


def test_unknown_limit_kind_still_surfaces():
    payload = {"limits": [{"kind": "monthly_thing", "percent": 3, "severity": "warning"}]}
    windows, _, _ = parse_oauth_usage(payload)
    assert windows[0].label == "Monthly thing"
    assert windows[0].severity == "warning"


def test_dollar_caps_produce_their_own_window():
    payload = {
        "five_hour": {
            "utilization": 50.0,
            "resets_at": "2026-08-02T07:20:00+00:00",
            "used_dollars": 2.5,
            "limit_dollars": 10.0,
        }
    }
    windows, _, _ = parse_oauth_usage(payload)
    dollars = next(w for w in windows if w.unit == "dollars")
    assert (dollars.used, dollars.limit, dollars.percent) == (2.5, 10.0, 25.0)


def test_extra_usage_and_spend_become_notes():
    payload = {
        "limits": [{"kind": "session", "percent": 1}],
        "extra_usage": {"is_enabled": True, "used_credits": 3.5, "currency": "USD"},
        "spend": {
            "used": {"amount_minor": 1234, "currency": "USD", "exponent": 2},
            "limit": {"amount_minor": 5000, "currency": "USD", "exponent": 2},
        },
    }
    _, _, notes = parse_oauth_usage(payload)
    assert notes == [
        "extra usage on: 3.50 USD of credits used",
        "spend this period: 12.34 USD of 50.00 USD",
    ]


def test_garbage_payload_yields_no_windows_instead_of_crashing():
    windows, plan, notes = parse_oauth_usage({"limits": "nope", "five_hour": 7})
    assert (windows, plan, notes) == ([], None, [])


# ---------------------------------------------------------------------------------------
# Source precedence: the live API wins, transcripts are strictly a fallback
# ---------------------------------------------------------------------------------------


def test_live_api_is_primary_and_transcripts_are_not_used(tmp_path, monkeypatch):
    projects = _seed_transcripts(tmp_path)
    payload = json.loads(FIXTURE.read_text())

    monkeypatch.setattr(claude_module, "load_claude_credential", lambda path: _credential())
    monkeypatch.setattr(claude_module, "fetch_oauth_usage", lambda credential: payload)
    monkeypatch.setattr(
        claude_module,
        "_iter_events",
        lambda data_dir: pytest.fail("transcripts must not be read while the live API works"),
    )

    snapshot = ClaudeProvider(ClaudeConfig(data_dir=projects)).fetch()

    assert snapshot.plan == "pro"
    assert snapshot.errors == []
    assert [w.source for w in snapshot.windows] == [OAUTH_SOURCE, OAUTH_SOURCE]
    assert all(w.confidence == Confidence.AUTHORITATIVE for w in snapshot.windows)

    # Only the source actually tried gets an attempt recorded.
    assert [a.name for a in snapshot.attempts] == [OAUTH_SOURCE]
    assert snapshot.attempts[0].outcome == SourceOutcome.OK


def test_transcript_fallback_activates_when_credential_is_missing(tmp_path, monkeypatch):
    projects = _seed_transcripts(tmp_path)

    monkeypatch.setattr(claude_module, "load_claude_credential", _no_credential)
    monkeypatch.setattr(
        claude_module,
        "fetch_oauth_usage",
        lambda credential: pytest.fail("must not call the API without a credential"),
    )

    snapshot = ClaudeProvider(ClaudeConfig(data_dir=projects)).fetch()

    assert all(w.source == TRANSCRIPT_SOURCE for w in snapshot.windows)
    assert all(w.confidence == Confidence.UNAVAILABLE for w in snapshot.windows)
    assert any("no Claude Code OAuth credential" in e for e in snapshot.errors)
    assert any("falling back to local transcript estimate" in e for e in snapshot.errors)


def test_transcript_fallback_activates_when_endpoint_fails(tmp_path, monkeypatch):
    projects = _seed_transcripts(tmp_path)

    def _boom(credential):
        raise UsageSourceError("returned HTTP 500")

    monkeypatch.setattr(claude_module, "load_claude_credential", lambda path: _credential())
    monkeypatch.setattr(claude_module, "fetch_oauth_usage", _boom)

    snapshot = ClaudeProvider(ClaudeConfig(data_dir=projects)).fetch()

    assert [w.source for w in snapshot.windows] == [TRANSCRIPT_SOURCE, TRANSCRIPT_SOURCE]
    assert any("HTTP 500" in e for e in snapshot.errors)


def test_expired_credential_degrades_without_calling_the_endpoint(tmp_path, monkeypatch):
    projects = _seed_transcripts(tmp_path)
    expired = _credential(expires_at=datetime.now(UTC) - timedelta(minutes=1))

    monkeypatch.setattr(claude_module, "load_claude_credential", lambda path: expired)
    monkeypatch.setattr(
        claude_module,
        "fetch_oauth_usage",
        lambda credential: pytest.fail("must not call the API with an expired token"),
    )

    snapshot = ClaudeProvider(ClaudeConfig(data_dir=projects)).fetch()
    assert any("expired" in e for e in snapshot.errors)
    assert all(w.source == TRANSCRIPT_SOURCE for w in snapshot.windows)


def test_unrecognized_live_payload_degrades_instead_of_reporting_nothing(tmp_path, monkeypatch):
    projects = _seed_transcripts(tmp_path)
    monkeypatch.setattr(claude_module, "load_claude_credential", lambda path: _credential())
    monkeypatch.setattr(claude_module, "fetch_oauth_usage", lambda credential: {"surprise": 1})

    snapshot = ClaudeProvider(ClaudeConfig(data_dir=projects)).fetch()
    assert any("no recognizable windows" in e for e in snapshot.errors)
    assert all(w.source == TRANSCRIPT_SOURCE for w in snapshot.windows)


def test_disabling_the_api_skips_credentials_entirely(tmp_path, monkeypatch):
    projects = _seed_transcripts(tmp_path)
    monkeypatch.setattr(
        claude_module,
        "load_claude_credential",
        lambda path: pytest.fail("must not read credentials when the API is disabled"),
    )

    snapshot = ClaudeProvider(
        ClaudeConfig(data_dir=projects, use_oauth_usage_api=False)
    ).fetch()
    assert any("disabled" in e for e in snapshot.errors)
    assert all(w.source == TRANSCRIPT_SOURCE for w in snapshot.windows)


# ---------------------------------------------------------------------------------------
# The transcript fallback itself
# ---------------------------------------------------------------------------------------


def test_no_data_dir_reports_unavailable(tmp_path):
    provider = ClaudeProvider(
        ClaudeConfig(data_dir=tmp_path / "missing", use_oauth_usage_api=False)
    )
    snapshot = provider.fetch()
    assert snapshot.provider == "claude"
    assert snapshot.errors
    assert snapshot.windows == []


# ---------------------------------------------------------------------------------------
# Structured source attempts and the `doctor` audit trail
# ---------------------------------------------------------------------------------------


def test_all_sources_failing_still_yields_full_attempt_list(tmp_path, monkeypatch):
    """Every source failing must produce a complete audit trail, not an empty/crashed result."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(claude_module, "load_claude_credential", _no_credential)

    provider = ClaudeProvider(ClaudeConfig(data_dir=tmp_path / "missing"))
    snapshot = provider.fetch()

    assert [a.name for a in snapshot.attempts] == [OAUTH_SOURCE, TRANSCRIPT_SOURCE]
    assert not any(a.ok for a in snapshot.attempts)
    assert snapshot.status == ProviderStatus.UNAVAILABLE

    live, transcripts = snapshot.attempts
    assert live.outcome == SourceOutcome.NO_CREDENTIAL
    assert "no Claude Code OAuth credential" in live.detail
    assert transcripts.outcome == SourceOutcome.NOT_FOUND
    assert "missing" in transcripts.detail

    # Every failed attempt has to tell the user what to actually do about it.
    assert all(a.remediation for a in snapshot.attempts)
    assert all(a.duration_ms >= 0 for a in snapshot.attempts)


def test_transcripts_present_but_unparseable_is_empty_not_not_found(tmp_path, monkeypatch):
    """Schema drift (files exist, no usage records) must be distinguishable from 'never used'."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(claude_module, "load_claude_credential", _no_credential)
    data_dir = tmp_path / "projects" / "p"
    data_dir.mkdir(parents=True)
    _write_jsonl(data_dir / "session.jsonl", [{"timestamp": "2026-01-01T00:00:00+00:00"}])

    provider = ClaudeProvider(ClaudeConfig(data_dir=tmp_path / "projects"))
    attempt = next(a for a in provider.fetch().attempts if a.name == TRANSCRIPT_SOURCE)

    assert attempt.outcome == SourceOutcome.EMPTY
    assert "1 transcript file(s)" in attempt.detail
    assert "schema may have changed" in attempt.remediation


def test_successful_transcript_source_records_ok_attempt(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(claude_module, "load_claude_credential", _no_credential)
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
    attempt = next(
        a for a in ClaudeProvider(config).fetch().attempts if a.name == TRANSCRIPT_SOURCE
    )

    assert attempt.outcome == SourceOutcome.OK
    assert attempt.ok
    assert "1 usage record(s)" in attempt.detail
    assert attempt.remediation is None


def test_source_exception_becomes_error_attempt(tmp_path, monkeypatch):
    """A source that blows up degrades to an `error` attempt instead of killing the snapshot."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(claude_module, "load_claude_credential", _no_credential)
    monkeypatch.setattr(
        claude_module,
        "_transcript_source",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    snapshot = ClaudeProvider(ClaudeConfig(data_dir=tmp_path)).fetch()
    attempt = next(a for a in snapshot.attempts if a.name == TRANSCRIPT_SOURCE)

    assert attempt.outcome == SourceOutcome.ERROR
    assert "RuntimeError: boom" in attempt.detail
    assert snapshot.status == ProviderStatus.UNAVAILABLE


def test_sums_tokens_in_active_block(tmp_path, monkeypatch):
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

    provider = ClaudeProvider(
        ClaudeConfig(
            data_dir=tmp_path / "projects",
            session_token_limit=1000,
            use_oauth_usage_api=False,
        )
    )
    snapshot = provider.fetch()

    session_window = next(w for w in snapshot.windows if "session" in w.label)
    assert session_window.used == 150
    assert session_window.limit == 1000
    assert session_window.confidence == Confidence.ESTIMATED

    weekly_window = next(w for w in snapshot.windows if "7d" in w.label)
    assert weekly_window.used == 1150
    assert weekly_window.confidence == Confidence.UNAVAILABLE


def test_malformed_lines_are_skipped(tmp_path):
    data_dir = tmp_path / "projects" / "p"
    data_dir.mkdir(parents=True)
    (data_dir / "session.jsonl").write_text("not json\n{}\n")

    provider = ClaudeProvider(
        ClaudeConfig(data_dir=tmp_path / "projects", use_oauth_usage_api=False)
    )
    snapshot = provider.fetch()
    assert snapshot.errors
