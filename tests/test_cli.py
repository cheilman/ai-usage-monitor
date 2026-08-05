import json
import os
import subprocess
import sys


def _env(tmp_path):
    """An environment where no source can succeed, so results are deterministic."""
    env = {
        **os.environ,
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-home"),
        "AI_USAGE_MONITOR_CONFIG": str(tmp_path / "no-config.toml"),
        # Pin rich's width so doctor output doesn't wrap mid-assertion.
        "COLUMNS": "200",
    }
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def _run(tmp_path, *args):
    return subprocess.run(
        [sys.executable, "-m", "ai_usage_monitor", *args],
        capture_output=True,
        text=True,
        env=_env(tmp_path),
        check=False,
    )


def test_snapshot_json_smoke(tmp_path):
    result = _run(tmp_path, "snapshot", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    providers = {entry["provider"] for entry in payload}
    assert providers == {"claude", "gemini"}


def test_snapshot_json_includes_attempts(tmp_path):
    result = _run(tmp_path, "snapshot", "--json")
    assert result.returncode == 0, result.stderr
    payload = {entry["provider"]: entry for entry in json.loads(result.stdout)}

    for provider, expected_sources in (("claude", 2), ("gemini", 1)):
        entry = payload[provider]
        assert entry["status"] == "unavailable"
        assert len(entry["attempts"]) == expected_sources
        for attempt in entry["attempts"]:
            assert set(attempt) == {"name", "outcome", "detail", "duration_ms", "remediation"}
            assert attempt["outcome"] != "ok"
            assert attempt["remediation"]


def test_doctor_lists_sources_and_exits_nonzero_when_all_fail(tmp_path):
    result = _run(tmp_path, "doctor")
    # No source can work in this env, so doctor reports failure for scripts to gate on.
    assert result.returncode == 1, result.stderr
    assert "CLAUDE sources" in result.stdout
    assert "GEMINI sources" in result.stdout
    assert "no_credential" in result.stdout
    assert "not_found" in result.stdout
    assert "0/2 source(s) healthy" in result.stdout


def test_doctor_single_provider_json(tmp_path):
    result = _run(tmp_path, "doctor", "--provider", "gemini", "--json")
    payload = json.loads(result.stdout)
    assert [entry["provider"] for entry in payload] == ["gemini"]
    assert payload[0]["attempts"][0]["outcome"] == "not_found"
