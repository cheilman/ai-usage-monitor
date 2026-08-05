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

# live btop-style dashboard, refreshing every 30s
ai-usage-monitor dashboard
ai-usage-monitor dashboard --interval 10
```

## Caching

Results are cached at `~/.cache/ai-usage-monitor/snapshot.json` (or
`$XDG_CACHE_HOME/ai-usage-monitor/snapshot.json`) for 60 seconds by default, per provider. The
windows being reported move over hours or days, so reusing a minute-old answer costs nothing in
accuracy and keeps a dashboard left open all day from repeatedly poking provider endpoints.

```sh
ai-usage-monitor --max-age 300     # reuse anything less than 5 minutes old
ai-usage-monitor --max-age 0       # always fetch live, but still refresh the cache
ai-usage-monitor --no-cache        # ignore the cache file completely; don't write it either
```

Both flags work on `snapshot` and `dashboard`. Each panel's footer shows when its data was
actually fetched (`as of 09:15:02 UTC (43s ago)`), so cached numbers are never mistaken for
live ones. The cache holds only the same fields `--json` prints — never API keys or tokens.
`AI_USAGE_MONITOR_CACHE` overrides the cache file path; deleting the file is always safe.

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
