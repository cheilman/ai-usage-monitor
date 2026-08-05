import json
import os
import subprocess
import sys

from ai_usage_monitor.cli import build_parser


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


def test_cache_flags_available_on_every_entry_point():
    """snapshot, dashboard, and the bare no-subcommand form must all accept the cache flags."""
    parser = build_parser()

    for argv in ([], ["snapshot"], ["dashboard"]):
        args = parser.parse_args(argv)
        assert args.max_age == 60.0
        assert args.no_cache is False

        overridden = parser.parse_args([*argv, "--max-age", "5", "--no-cache"])
        assert overridden.max_age == 5.0
        assert overridden.no_cache is True


def test_dashboard_interval_default_is_30s():
    """Paired with the 60s cache TTL: two ticks per round of provider requests."""
    assert parser_default("dashboard", "interval") == 30.0


def parser_default(command: str, attr: str):
    return getattr(build_parser().parse_args([command]), attr)
