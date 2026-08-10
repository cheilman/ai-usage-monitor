"""End-to-end CLI checks: the real binary, the real document, the real exit codes."""

import json
import os
import subprocess
import sys

from ai_usage_monitor.cli import build_parser
from test_json_contract import assert_valid_v1

# Both sources pointed at nothing, so the run is hermetic: no keychain, no network, no
# dependence on whether the developer's own machine happens to have usage data lying around.
OFFLINE_CONFIG = """\
[claude]
use_oauth_usage_api = false

[gemini]
telemetry_log = "{telemetry_log}"
"""


def run_cli(tmp_path, *args):
    config = tmp_path / "config.toml"
    config.write_text(OFFLINE_CONFIG.format(telemetry_log=tmp_path / "no-telemetry.log"))
    env = {
        **os.environ,
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-home"),
        "AI_USAGE_MONITOR_CONFIG": str(config),
        "CLAUDE_CREDENTIALS_FILE": str(tmp_path / "no-credentials.json"),
    }
    env.pop("ANTHROPIC_API_KEY", None)
    return subprocess.run(
        [sys.executable, "-m", "ai_usage_monitor", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_snapshot_json_emits_a_valid_v1_document(tmp_path):
    result = run_cli(tmp_path, "snapshot", "--json")

    document = json.loads(result.stdout)
    assert_valid_v1(document)
    assert document["schema_version"] == 1

    providers = {p["provider"] for p in document["providers"]}
    assert providers == {"claude", "gemini"}

    claude = next(p for p in document["providers"] if p["provider"] == "claude")
    assert "plan" in claude and "notes" in claude
    assert any("live usage API unavailable" in error for error in claude["errors"])

    # Every source was cut off, so quota is *unknown* -- and that must show up as null plus a
    # non-zero exit, never as an empty-but-successful "you're fine".
    assert document["most_constrained"] is None
    assert {p["status"] for p in document["providers"]} == {"unavailable"}
    assert result.returncode == 2, result.stderr


def test_bare_invocation_accepts_the_same_flags_as_snapshot(tmp_path):
    result = run_cli(tmp_path, "--json", "--fail-on-degraded")

    assert_valid_v1(json.loads(result.stdout))
    assert result.returncode == 2, result.stderr


def test_human_output_explains_a_non_zero_exit(tmp_path):
    result = run_cli(tmp_path, "snapshot")

    assert result.returncode == 2
    assert "exit 2" in result.stderr


def test_help_documents_the_exit_codes(tmp_path):
    result = run_cli(tmp_path, "--help")

    assert result.returncode == 0
    assert "exit codes:" in result.stdout
    assert "--fail-on-degraded" in result.stdout


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
