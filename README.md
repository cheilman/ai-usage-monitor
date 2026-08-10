# ai-usage-monitor

Terminal tool that reports current usage, plan caps (where known), and reset timing for Claude,
Gemini, and OpenRouter, so you don't have to dig through each provider's own UI.

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

# non-zero exit unless *every* provider is reporting authoritatively
ai-usage-monitor --json --fail-on-degraded

# just one provider
ai-usage-monitor snapshot --provider claude
ai-usage-monitor snapshot --provider openrouter

# live btop-style dashboard, refreshing every 30s
ai-usage-monitor dashboard
ai-usage-monitor dashboard --interval 10

# why is a number missing? show every source tried, per provider
ai-usage-monitor doctor
ai-usage-monitor doctor --provider gemini --json
```

## Diagnosing missing numbers

Every source this tool reads is an undocumented internal format or an unpublished endpoint, so
sources break. `doctor` reports each one tried, per provider, with an outcome (`ok`, `empty`,
`not_found`, `no_credential`, `error`), the detail behind it, how long it took, and what to do
about it:

```
╭─ CLAUDE sources ──────────────────────────────────────────────────────────╮
│ [-] GET /api/oauth/usage (Claude Code OAuth token)  no_credential  (4ms)   │
│       no Claude Code OAuth credential found (looked in keychain)          │
│       -> Run `claude` once so Claude Code stores a fresh OAuth credential.│
│                                                                           │
│ [OK] local JSONL transcript scrape (fallback; ~/.claude/projects)  ok  (2ms) │
│       1 usage record(s) across 1 transcript file(s)                       │
│                                                                           │
│ 1/2 source(s) healthy; overall status: unavailable                        │
╰───────────────────────────────────────────────────────────────────────────╯
```

Only sources actually tried show up: the transcript fallback is skipped entirely (no attempt
recorded) whenever the OAuth source above it already succeeded. `doctor` exits non-zero if any
selected provider has no healthy source, so it's usable as a check in scripts. The same
attempts appear in the `attempts` array of any `--json` output.

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

## JSON contract (`schema_version` 1)

`--json` emits a versioned envelope, not a bare array. It is a stable interface: fields are
added compatibly, and anything that breaks a consumer bumps `schema_version`. Parse
defensively — refuse a `schema_version` you don't recognize rather than guessing.

```json
{
  "schema_version": 1,
  "generated_at": "2026-08-05T02:45:28.104913+00:00",
  "providers": [
    {
      "provider": "claude",
      "status": "ok",
      "fetched_at": "2026-08-05T02:45:28.104110+00:00",
      "plan": "pro",
      "windows": [
        {
          "key": "five_hour",
          "label": "5-hour session",
          "unit": "percent",
          "used": 21.0,
          "limit": 100.0,
          "percent": 21.0,
          "reset_at": "2026-08-05T07:20:00.628697+00:00",
          "confidence": "authoritative",
          "source": "GET /api/oauth/usage (Claude Code OAuth token)",
          "note": null,
          "is_active": false,
          "severity": "normal"
        }
      ],
      "attempts": [
        {
          "name": "GET /api/oauth/usage (Claude Code OAuth token)",
          "outcome": "ok",
          "detail": "2 window(s), plan=pro",
          "duration_ms": 182.4,
          "remediation": null
        }
      ],
      "notes": [],
      "errors": []
    }
  ],
  "most_constrained": {
    "provider": "claude",
    "key": "weekly_all",
    "label": "Weekly (all models)",
    "utilization_pct": 54.0,
    "resets_at": "2026-08-06T00:00:00.628717+00:00"
  }
}
```

### `most_constrained`

The single window closest to its cap across every provider, so a throttling caller is one
lookup rather than a reimplementation of the precedence rules. Highest utilization wins; ties
go to the better `confidence`, then to provider/key alphabetically, so the answer is
deterministic. Windows with an unknown percentage, and windows marked `unavailable`, are not
candidates.

> `most_constrained` is `null` when **nothing** is known. That means quota is *unknown* — it
> does **not** mean quota is available. Treat `null` as a reason to back off or to check
> another way, never as a green light.

### `key` vs `label`

Match on `key`. It's a stable machine identifier, unique within a provider; `label` is display
prose and may be reworded at any time. Claude spells its 5-hour window `session` in one part
of the upstream payload and `five_hour` in another — both normalize onto `five_hour`, so the
key holds whichever branch produced the row.

| Provider | `key` | Window |
| -------- | ----- | ------ |
| claude | `five_hour` | rolling 5-hour session utilization |
| claude | `weekly_all` | rolling weekly utilization, all models |
| claude | `weekly_opus` / `weekly_sonnet` | per-model weekly utilization |
| claude | `*_dollars` | the same windows in dollars, on credit/overage plans |
| claude | `session_tokens` / `weekly_tokens` | **fallback only** — raw transcript token counts against a hand-configured cap, deliberately *not* keyed as the authoritative windows |
| claude | `api_tokens_per_minute` | `ANTHROPIC_API_KEY` rate limit — a different thing from subscription usage |
| gemini | `daily_requests` / `daily_tokens` | today's requests / tokens |
| openrouter | `credit_limit_<label>` | spend against that key's configured credit cap |
| openrouter | `usage_<label>` | all-time spend, for keys with no cap configured |

An unrecognized upstream window kind gets a slugified key rather than being dropped, so a new
plan shape shows up as an extra row.

### Provider `status` and exit codes

| `status` | Meaning |
| -------- | ------- |
| `ok` | at least one authoritative window, and nothing in the source chain failed |
| `degraded` | usable numbers, but from a fallback or a driftable estimate |
| `unavailable` | no usable window at all — quota unknown |

| Exit | Condition |
| ---- | --------- |
| `0` | at least one provider is `ok` |
| `2` | no provider is `ok` — or, with `--fail-on-degraded`, any provider isn't |

So `ai-usage-monitor --json || back_off` is a valid gate on its own, without parsing.

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

**OpenRouter** is the one provider with a real, documented, key-scoped usage endpoint:
`GET https://openrouter.ai/api/v1/key`, authenticated with `Authorization: Bearer <api key>`.
Every window it produces is `authoritative` — there's no fallback source, because none is
needed. Every `OPENROUTER_API_KEY` (→ label `default`) or `OPENROUTER_API_KEY_<LABEL>`
(→ label `<label>`) environment variable becomes its own row, so tracking several keys (a
personal one, a work one, a CI token) is a matter of exporting more variables, not editing
config. One revoked or misconfigured key reports its own error without hiding the others.

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

OpenRouter needs no config file entry at all — only environment variables, the same rule
`ANTHROPIC_API_KEY` follows, because a secret doesn't belong in a config file:

```sh
export OPENROUTER_API_KEY=sk-or-...              # -> reported as "default"
export OPENROUTER_API_KEY_WORK=sk-or-...         # -> reported as "work"
```

Set none and the provider reports `unavailable` with instructions; set several and each gets
its own row and its own `doctor` attempt, so one bad key can't hide the others.

## Development

```sh
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```
