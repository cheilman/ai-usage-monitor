# Design notes

## Why there's no clean "get my usage" API

Neither Anthropic nor Google publish a simple endpoint for "how much of my plan have I used."
Researched during this pass:

**Claude**
- The `claude.ai/settings/usage` page and Claude Code's `/usage` command show 5-hour session +
  weekly usage, but there is no documented public API behind them.
- Claude Code (Pro/Max/Team) writes one JSONL transcript file per session to
  `~/.claude/projects/<project>/<session-id>.jsonl`. Each line with a `message.usage` field
  carries `input_tokens` / `output_tokens` / `cache_creation_input_tokens` /
  `cache_read_input_tokens`. Community tools (`ccusage`) already build on this, and this
  project does the same to approximate the rolling 5-hour session window and a trailing 7-day
  window.
- The plan's actual token cap for that 5-hour/weekly window is **not published anywhere
  readable locally or via API** (and changed at least once in 2026, per Anthropic's own
  changelog). We do not guess at it — `limit` is `None`/"unavailable" unless the user supplies
  it in config.
- If `ANTHROPIC_API_KEY` is set, direct API calls (not Claude Code subscription usage) *do*
  return authoritative `anthropic-ratelimit-{requests,tokens}-{limit,remaining,reset}` response
  headers on every call. We piggyback on the free `/v1/messages/count_tokens` endpoint to read
  these without spending completion tokens, and report it as a distinct window since it
  measures a different thing (API tier rate limit, not subscription session usage).

**Gemini**
- The Gemini CLI's own `/stats model` command shows session tokens and quota, but only
  interactively — nothing scriptable.
- The CLI can be configured to write local OpenTelemetry logs (`~/.gemini/settings.json` →
  `telemetry.target = "local"`), which include `gemini_cli.api_response` events with token
  counts. This is **off by default**, so if the log file isn't there we report the window as
  `unavailable` with the exact settings.json snippet needed to turn it on, rather than
  pretending we have data.
- Request caps for a given tier (e.g. free-tier daily request limits) are not exposed via any
  API or local file at all. They come from `daily_request_limit` in config, seeded from
  gemini-cli's published quota docs — this is a hardcoded, driftable value and is flagged as
  `estimated` confidence, never `authoritative`.

## Fragility, called out explicitly

Both local-file fallbacks (`~/.claude/projects/*.jsonl`, `~/.gemini/telemetry.log`) are
undocumented-for-third-party-use internal formats owned by the respective CLIs. They can change
schema, move location, or disappear in a future CLI release without notice, silently breaking
this tool. Every `UsageWindow` carries a `confidence` field
(`authoritative` / `estimated` / `unavailable`) and a `source` string precisely so a consumer
(human or script) can tell live-API data from best-effort log scraping from "we don't know" —
see `src/ai_usage_monitor/models.py`.

## Source attempts and `doctor`

Confidence flags say how good a number is, but not why a number is *missing*. Since every
source here will eventually break, each provider records a `SourceAttempt(name, outcome,
detail, duration_ms, remediation)` for **every** source it tries, successful or not, in
`ProviderSnapshot.attempts`. Outcomes are `ok` / `empty` / `not_found` / `no_credential` /
`error`, and a source that raises unexpectedly is caught and downgraded to an `error` attempt
rather than taking the whole snapshot down (`providers/base.py:run_source`).

`ai-usage-monitor doctor` prints that trail per provider with a remediation line, and exits
non-zero when a provider has no healthy source. The distinction that matters most is
`not_found` (never used / wrong path) versus `empty` (the file is there but carries no records
we recognise) — the latter is what schema drift looks like from the outside, and previously
both just rendered as "no data".

`ProviderSnapshot.errors` is reserved for genuine, provider-level failures — "nothing worked at
all" — rather than per-source explanations, which now live in `attempts`.

## Layout

- `providers/claude.py`, `providers/gemini.py` — one `fetch() -> ProviderSnapshot` per provider,
  each running an ordered chain of sources through `run_source`.
- `providers/base.py` — the `UsageProvider` protocol and `run_source`, which times one source
  and turns its result (or its exception) into a `SourceAttempt`.
- `config.py` — optional `~/.config/ai-usage-monitor/config.toml` for plan caps that aren't
  discoverable programmatically.
- `render.py` — shared rendering (rich panels + JSON) used by all CLI modes, so snapshot,
  dashboard, and doctor never drift apart.
- `cli.py` — `snapshot` (default, supports `--json`), `doctor` (per-source diagnostics), and
  `dashboard` (live, `rich.Live`) subcommands.
