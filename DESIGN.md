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

## Caching

Every source we read is either an undocumented endpoint or someone else's local log format, and
the dashboard re-reads them on a timer. So snapshots go through an on-disk cache
(`~/.cache/ai-usage-monitor/snapshot.json`, 60s default TTL, `--max-age S` / `--no-cache` to
override), keyed by provider so a single-provider run doesn't invalidate the other's data.

The accuracy cost is ~zero: a rolling 5-hour window, a 7-day window and a daily request count
do not meaningfully change in 60 seconds. The benefit is that leaving the dashboard open no
longer means one request per provider every refresh — with the default 30s interval and 60s
max-age it's one round of requests per minute regardless of how fast the screen redraws.

Cache failures are always *misses*, never errors: an unreadable, truncated, hand-mangled or
future-versioned file just triggers a live fetch (and gets overwritten with a good one). Writes
are atomic (temp file + `os.replace`) and best-effort — an unwritable cache dir must not break
the tool. The stored payload is exactly `snapshot_to_dict` output, which contains no
credentials, because `ProviderSnapshot` has nowhere to put one.

Because cached data is by definition not "now", the panel footer reports the snapshot's own
`fetched_at` plus an age (`as of 09:15:02 UTC (43s ago)`) rather than the redraw time.

## Layout

- `providers/claude.py`, `providers/gemini.py` — one `fetch() -> ProviderSnapshot` per provider.
- `cache.py` — TTL'd on-disk snapshot cache plus `fetch_snapshots()`, the single read-through
  entry point both CLI modes use.
- `config.py` — optional `~/.config/ai-usage-monitor/config.toml` for plan caps that aren't
  discoverable programmatically.
- `render.py` — shared rendering (rich panels + JSON) and the snapshot (de)serializer used by
  both CLI modes and the cache, so snapshot and dashboard never drift apart.
- `cli.py` — `snapshot` (default, supports `--json`) and `dashboard` (live, `rich.Live`)
  subcommands.
