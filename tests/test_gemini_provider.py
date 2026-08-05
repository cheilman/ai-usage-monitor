import json
from datetime import UTC, datetime
from pathlib import Path

from ai_usage_monitor.config import GeminiConfig
from ai_usage_monitor.models import Confidence, SourceOutcome
from ai_usage_monitor.providers.gemini import TELEMETRY_SOURCE, GeminiProvider


def test_missing_log_reports_unavailable(tmp_path: Path):
    provider = GeminiProvider(GeminiConfig(telemetry_log=tmp_path / "missing.log"))
    snapshot = provider.fetch()
    assert snapshot.errors
    assert snapshot.windows[0].confidence == Confidence.UNAVAILABLE


def test_missing_log_records_attempt_with_remediation(tmp_path: Path):
    snapshot = GeminiProvider(GeminiConfig(telemetry_log=tmp_path / "missing.log")).fetch()

    assert len(snapshot.attempts) == 1
    attempt = snapshot.attempts[0]
    assert attempt.name == TELEMETRY_SOURCE
    assert attempt.outcome == SourceOutcome.NOT_FOUND
    assert "missing.log" in attempt.detail
    assert "telemetry" in attempt.remediation
    assert snapshot.status == Confidence.UNAVAILABLE


def test_log_without_usable_events_reports_empty(tmp_path: Path):
    log_path = tmp_path / "telemetry.log"
    log_path.write_text('{"name": "gemini_cli.something_else"}\n')

    attempt = GeminiProvider(GeminiConfig(telemetry_log=log_path)).fetch().attempts[0]

    assert attempt.outcome == SourceOutcome.EMPTY
    assert "1 line(s)" in attempt.detail


def test_counts_todays_requests(tmp_path: Path):
    log_path = tmp_path / "telemetry.log"
    now = datetime.now(UTC)
    line = json.dumps(
        {
            "name": "gemini_cli.api_response",
            "timestamp": now.isoformat(),
            "attributes": {"input_token_count": 10, "output_token_count": 20},
        }
    )
    log_path.write_text(line + "\n")

    provider = GeminiProvider(GeminiConfig(telemetry_log=log_path, daily_request_limit=100))
    snapshot = provider.fetch()

    requests_window = next(w for w in snapshot.windows if "requests" in w.label)
    assert requests_window.used == 1
    assert requests_window.limit == 100

    tokens_window = next(w for w in snapshot.windows if "tokens" in w.label)
    assert tokens_window.used == 30

    assert snapshot.attempts[0].outcome == SourceOutcome.OK
    assert snapshot.errors == []
