import json
import os
import subprocess
import sys


def test_snapshot_json_smoke(tmp_path):
    env = {
        **os.environ,
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-home"),
        "AI_USAGE_MONITOR_CONFIG": str(tmp_path / "no-config.toml"),
    }
    env.pop("ANTHROPIC_API_KEY", None)

    result = subprocess.run(
        [sys.executable, "-m", "ai_usage_monitor", "snapshot", "--json"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    providers = {entry["provider"] for entry in payload}
    assert providers == {"claude", "gemini"}
