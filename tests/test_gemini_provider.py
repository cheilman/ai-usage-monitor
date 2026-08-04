import json
from datetime import UTC, datetime
from pathlib import Path

from ai_usage_monitor.config import GeminiConfig
from ai_usage_monitor.models import Confidence
from ai_usage_monitor.providers.gemini import GeminiProvider


def test_missing_log_reports_unavailable(tmp_path: Path):
    provider = GeminiProvider(GeminiConfig(telemetry_log=tmp_path / "missing.log"))
    snapshot = provider.fetch()
    assert snapshot.errors
    assert snapshot.windows[0].confidence == Confidence.UNAVAILABLE


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
