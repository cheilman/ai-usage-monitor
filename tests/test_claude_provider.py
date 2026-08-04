import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ai_usage_monitor.config import ClaudeConfig
from ai_usage_monitor.models import Confidence
from ai_usage_monitor.providers.claude import ClaudeProvider


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records))


def test_no_data_dir_reports_unavailable(tmp_path: Path):
    provider = ClaudeProvider(ClaudeConfig(data_dir=tmp_path / "missing"))
    snapshot = provider.fetch()
    assert snapshot.provider == "claude"
    assert snapshot.errors
    assert snapshot.windows == []


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
