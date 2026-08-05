# ai-usage-monitor

Terminal tool that reports current usage, plan caps (where known), and reset timing for Claude
and Gemini, so you don't have to dig through each provider's own UI.

Claude works with **no configuration**: usage comes from the same OAuth-scoped endpoint Claude
Code itself calls, authenticated with the token already in your keychain, and the percentages
it returns are already normalized against your plan's real cap.

```
╭──────────────── CLAUDE plan: pro ─────────────────╮
│ 5-hour session *          81% 2h04m authoritative │
│ ━━━━━━━━━━━━━━━━━━━━━━━━━                         │
│ Weekly (all models)       14% 4d21h authoritative │
│ ━━━━                                              │
╰────────────────────────────── as of 02:45:28 UTC ─╯
```

See [DESIGN.md](DESIGN.md) for the per-provider source chain, and for why some numbers are
marked `estimated` or `unavailable` rather than guessed at.

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
```

## Where the numbers come from

**Claude** tries two sources in order and takes the first that works:

1. `GET https://api.anthropic.com/api/oauth/usage` — Claude Code's own usage endpoint, using
   the OAuth token from the macOS keychain (service `Claude Code-credentials`) or
   `~/.claude/.credentials.json`. Returns real 5-hour and weekly utilization, reset times,
   severity, and your plan name. Marked `authoritative`. Needs no config.
   The first run may raise a one-time keychain access prompt; the token is only ever read,
   never refreshed or written back.
2. Local `~/.claude/projects/**/*.jsonl` transcripts — a **fallback** for when the keychain
   isn't available (non-macOS, headless) or the credential is missing/expired. It can count
   tokens but can't know your cap, so it reports `unavailable` unless you supply one, and the
   snapshot's `errors` explain why the live source was skipped.

If `ANTHROPIC_API_KEY` is set you also get an `API tokens/min` window from the
`anthropic-ratelimit-*` response headers. That's your **API-key** rate limit, a different thing
from subscription usage, which is why it's a separate row.

**Gemini** has no reachable quota API, so it depends on local telemetry (below).

## Config

Optional — Claude's primary path needs none of it.

```sh
mkdir -p ~/.config/ai-usage-monitor
cat > ~/.config/ai-usage-monitor/config.toml <<'EOF'
[claude]
# use_oauth_usage_api = false      # stay fully offline: no keychain read, no HTTP
# credentials_file = "~/.claude/.credentials.json"   # skip the keychain lookup
# These two apply ONLY to the degraded transcript fallback:
# session_token_limit = 0          # tokens per rolling 5h window; 0/unset = unknown
# weekly_token_limit = 0           # tokens per rolling 7d window; 0/unset = unknown

[gemini]
daily_request_limit = 1000
EOF
```

`AI_USAGE_MONITOR_CONFIG` overrides the config file path. `CLAUDE_CONFIG_DIR` overrides where
Claude Code's local session data is read from (default `~/.claude`). `CLAUDE_CREDENTIALS_FILE`
overrides the credential lookup.

Gemini local usage requires enabling telemetry in `~/.gemini/settings.json` first:

```json
{"telemetry": {"enabled": true, "target": "local", "outfile": "~/.gemini/telemetry.log"}}
```

## Development

```sh
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```
