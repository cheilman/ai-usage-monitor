# ai-usage-monitor

Terminal tool that reports current usage, plan caps (where known), and reset timing for Claude
and Gemini, so you don't have to dig through each provider's own UI.

See [DESIGN.md](DESIGN.md) for what data is actually available per provider and why some
numbers are marked `estimated` or `unavailable` rather than guessed at.

## Install

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Usage

```sh
# one-shot snapshot, both providers
ai-usage-monitor
ai-usage-monitor snapshot

# machine-readable, for scripts/other agents
ai-usage-monitor --json

# just one provider
ai-usage-monitor snapshot --provider claude

# live btop-style dashboard, refreshing every 5s
ai-usage-monitor dashboard
ai-usage-monitor dashboard --interval 2

# why is a number missing? show every source tried, per provider
ai-usage-monitor doctor
ai-usage-monitor doctor --provider gemini --json
```

## Diagnosing missing numbers

Every source this tool reads is an undocumented internal format or an unpublished endpoint, so
sources break. `doctor` reports each one it tried with an outcome (`ok`, `empty`, `not_found`,
`no_credential`, `error`), the detail behind it, how long it took, and what to do about it:

```
╭─ CLAUDE sources ──────────────────────────────────────────────────────────╮
│ [-] anthropic live rate-limit headers  no_credential  (0ms)               │
│       ANTHROPIC_API_KEY is not set                                        │
│       -> Export ANTHROPIC_API_KEY to see live API rate limits. ...        │
│                                                                           │
│ [OK] claude code JSONL transcripts  ok  (2ms)                             │
│       1 usage records across 1 transcript file(s)                         │
│                                                                           │
│ 1/2 source(s) healthy; overall status: estimated                          │
╰───────────────────────────────────────────────────────────────────────────╯
```

It exits non-zero if any selected provider has no healthy source, so it's usable as a check in
scripts. The same attempts appear in the `attempts` array of any `--json` output.

## Config

Plan caps aren't discoverable via any API, so set them yourself if you know them:

```sh
mkdir -p ~/.config/ai-usage-monitor
cat > ~/.config/ai-usage-monitor/config.toml <<'EOF'
[claude]
session_token_limit = 0   # tokens per rolling 5h window; 0 = unknown
weekly_token_limit = 0    # tokens per rolling 7d window; 0 = unknown

[gemini]
daily_request_limit = 1000
EOF
```

`AI_USAGE_MONITOR_CONFIG` overrides the config file path. `CLAUDE_CONFIG_DIR` overrides where
Claude Code's local session data is read from (default `~/.claude`).

Gemini local usage requires enabling telemetry in `~/.gemini/settings.json` first:

```json
{"telemetry": {"enabled": true, "target": "local", "outfile": "~/.gemini/telemetry.log"}}
```

## Development

```sh
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```
