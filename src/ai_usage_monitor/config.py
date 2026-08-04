"""User-editable config, since neither provider publishes plan caps programmatically.

Lives at $AI_USAGE_MONITOR_CONFIG or ~/.config/ai-usage-monitor/config.toml. All keys are
optional -- an absent cap just means we report usage with limit=None (confidence UNAVAILABLE)
instead of a percentage.
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
    session_token_limit: int | None = None
    weekly_token_limit: int | None = None
    session_window_hours: float = 5.0


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

    return Config(
        claude=ClaudeConfig(
            data_dir=claude_data_dir,
            session_token_limit=claude_raw.get("session_token_limit") or None,
            weekly_token_limit=claude_raw.get("weekly_token_limit") or None,
            session_window_hours=claude_raw.get("session_window_hours", 5.0),
        ),
        gemini=GeminiConfig(
            telemetry_log=gemini_log,
            daily_request_limit=gemini_raw.get("daily_request_limit", 1000),
        ),
    )


SAMPLE_CONFIG = """\
# ai-usage-monitor config
# Neither Anthropic nor Google publish subscription plan caps via API, so if you know
# your numbers (e.g. from claude.ai/settings/usage or your Gemini tier docs) put them
# here to get real percentages instead of "unavailable".

[claude]
# data_dir = "~/.claude"          # where Claude Code writes projects/*.jsonl (default shown)
session_token_limit = 0           # tokens allowed per rolling 5h window; 0 = unknown
weekly_token_limit = 0            # tokens allowed per rolling 7d window; 0 = unknown
session_window_hours = 5.0

[gemini]
# telemetry_log = "~/.gemini/telemetry.log"  # requires telemetry.target=local in gemini settings
daily_request_limit = 1000        # public free-tier default; change for paid tiers
"""
