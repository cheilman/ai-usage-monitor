"""User-editable config for the bits that aren't discoverable programmatically.

Lives at $AI_USAGE_MONITOR_CONFIG or ~/.config/ai-usage-monitor/config.toml. Every key is
optional, and the common case needs no config file at all: Claude's primary source
(/api/oauth/usage) reports utilization already normalized against the account's real cap.
The Claude token limits below only affect the degraded transcript fallback; an absent cap
there means we report usage with limit=None (confidence UNAVAILABLE) rather than guessing.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


def _default_config_path() -> Path:
    override = os.environ.get("AI_USAGE_MONITOR_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "ai-usage-monitor" / "config.toml"


@dataclass
class ClaudeConfig:
    data_dir: Path
    # Fallback-only: caps for the local transcript estimate, used when the live API is out
    # of reach. The live API needs neither.
    session_token_limit: int | None = None
    weekly_token_limit: int | None = None
    session_window_hours: float = 5.0
    # Primary source. Turn off to stay entirely offline (no keychain read, no HTTP).
    use_oauth_usage_api: bool = True
    # Explicit credential file; when set it replaces the keychain/default-path lookup.
    credentials_file: Path | None = None


@dataclass
class GeminiConfig:
    telemetry_log: Path
    daily_request_limit: int | None = 1000  # public free-tier default; override if paid/tiered


@dataclass
class Config:
    claude: ClaudeConfig
    gemini: GeminiConfig


def load_config(path: Path | None = None) -> Config:
    path = path or _default_config_path()
    raw: dict = {}
    if path.exists():
        with path.open("rb") as fh:
            raw = tomllib.load(fh)

    claude_raw = raw.get("claude", {})
    gemini_raw = raw.get("gemini", {})

    claude_data_dir = Path(
        os.environ.get("CLAUDE_CONFIG_DIR", claude_raw.get("data_dir", "~/.claude"))
    ).expanduser() / "projects"

    gemini_log = Path(
        gemini_raw.get("telemetry_log", "~/.gemini/telemetry.log")
    ).expanduser()

    credentials_file = claude_raw.get("credentials_file")

    return Config(
        claude=ClaudeConfig(
            data_dir=claude_data_dir,
            session_token_limit=claude_raw.get("session_token_limit") or None,
            weekly_token_limit=claude_raw.get("weekly_token_limit") or None,
            session_window_hours=claude_raw.get("session_window_hours", 5.0),
            use_oauth_usage_api=bool(claude_raw.get("use_oauth_usage_api", True)),
            credentials_file=Path(credentials_file).expanduser() if credentials_file else None,
        ),
        gemini=GeminiConfig(
            telemetry_log=gemini_log,
            daily_request_limit=gemini_raw.get("daily_request_limit", 1000),
        ),
    )


SAMPLE_CONFIG = """\
# ai-usage-monitor config -- entirely optional.
#
# Claude needs no configuration: usage comes from Anthropic's own /api/oauth/usage endpoint,
# authenticated with the token Claude Code already stored, and the percentages it returns are
# already normalized against your plan's real cap.
#
# Google publishes no equivalent, so the Gemini cap below is a hardcoded tier default.

[claude]
# data_dir = "~/.claude"           # where Claude Code writes projects/*.jsonl (default shown)
# use_oauth_usage_api = true       # false = never touch the keychain or network
# credentials_file = "~/.claude/.credentials.json"   # overrides the keychain lookup
#
# The two caps below apply ONLY to the degraded local-transcript fallback used when the live
# API is unreachable (non-macOS, no credential, offline). Leave unset to report "unavailable"
# rather than a guess.
# session_token_limit = 0          # tokens allowed per rolling 5h window; 0/unset = unknown
# weekly_token_limit = 0           # tokens allowed per rolling 7d window; 0/unset = unknown
# session_window_hours = 5.0

[gemini]
# telemetry_log = "~/.gemini/telemetry.log"  # requires telemetry.target=local in gemini settings
daily_request_limit = 1000        # public free-tier default; change for paid tiers
"""
