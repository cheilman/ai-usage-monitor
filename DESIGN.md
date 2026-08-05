# Design notes

## What each provider actually exposes

Neither provider *documents* a public "how much of my plan have I used" API. Claude does have
a working undocumented one; Google does not. That asymmetry drives the whole design, and each
provider declares an **ordered source chain** whose first success wins.

**Claude** — chain: `oauth_usage_api` → `transcripts` → unavailable.

1. **`GET https://api.anthropic.com/api/oauth/usage`** — the private, OAuth-scoped endpoint
   Claude Code itself calls. Confirmed live (HTTP 200) during the K-000076 design phase and
   again when this provider was implemented. Authenticated with the access token Claude Code
   already stored: macOS keychain, service `Claude Code-credentials` (JSON blob under
   `claudeAiOauth`), or `~/.claude/.credentials.json` on Linux. Headers:
   `Authorization: Bearer <token>` and `anthropic-beta: oauth-2025-04-20`.

   The response carries `five_hour` / `seven_day` / `seven_day_opus` / `seven_day_sonnet`
   utilization percentages with per-window `resets_at`, a pre-normalized `limits[]` array
   (`kind`, `percent`, `severity`, `is_active`, `resets_at`), plus `plan`, `extra_usage` and
   `spend`. **Utilization is already normalized against the account's real cap, so this path
   needs no user-supplied limits at all** — it is `authoritative`, and it is what you get out
   of the box with zero configuration.

   We prefer `limits[]` over the named keys because it is plan-shape-agnostic and carries
   `severity`/`is_active`; the named keys are the fallback if `limits[]` is absent or empty.
   Dollar caps (`used_dollars` / `limit_dollars`, present on credit/overage plans) become
   their own window.

2. **Local JSONL transcripts** — *fallback only*, used when step 1 can't run: no keychain
   (non-macOS, headless), no credential, an expired token, an HTTP error, or a payload whose
   shape we no longer recognize. Claude Code writes one JSONL file per session to
   `~/.claude/projects/<project>/<session-id>.jsonl`; each line with a `message.usage` field
   carries `input_tokens` / `output_tokens` / `cache_creation_input_tokens` /
   `cache_read_input_tokens`. We sum the active rolling block (gap-based split, the same
   heuristic the community `ccusage` tool uses) and the trailing 7 days.

   This gives token *counts* but **no plan cap** — the cap is not published anywhere readable
   locally, and it changed at least once in 2026. We do not guess: `limit` stays `None`
   ("unavailable") unless the user supplies `session_token_limit`/`weekly_token_limit` in
   config, in which case the window is `estimated`, never `authoritative`. Whenever this path
   runs, the snapshot's `errors` say why the live source was skipped.

**Not part of that chain:** if `ANTHROPIC_API_KEY` is set, direct API calls return
authoritative `anthropic-ratelimit-{requests,tokens}-{limit,remaining,reset}` response headers.
We piggyback on the free `/v1/messages/count_tokens` endpoint to read these without spending
completion tokens, and report them as a **separate, separately-labelled window** — API-key
rate limits are a different thing from subscription usage and must never be conflated with it.

**Credentials are read strictly read-only.** We never refresh, rotate, or write back a token:
another live process owns that store, and racing it could log the user out mid-session. An
expired token is reported as such (`run \`claude\` once to refresh`) and degrades to the
fallback. Tokens are never logged, never included in an error message, never serialized.

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

**Nothing here is a supported public API.** Every source can break without notice, so the
design's job is to fail loudly and legibly rather than to keep printing a confident number.

| Source | Fragility | Breaks when | Detection | Behaviour on break |
| ------ | --------- | ----------- | --------- | ------------------ |
| Claude `/api/oauth/usage` | undocumented private endpoint; **verified working** | Anthropic changes path/shape/beta header; token expires; keychain access denied | HTTP != 200, non-JSON body, no recognizable windows, `expiresAt` in the past | fall through to transcripts, record the reason in `errors` |
| Claude keychain credential | undocumented blob layout | Claude Code renames the service or the `claudeAiOauth` keys | missing key → `CredentialError` naming the field | fall through to transcripts |
| Claude transcripts JSONL | best-effort local file format | Claude Code changes transcript layout or location | missing dirs, no `message.usage` keys | tokens only, never a limit; `unavailable` without a configured cap |
| `anthropic-ratelimit-*` headers | documented, but measures API-key limits | header names change | header absent | window simply omitted |
| Gemini `telemetry.log` | off by default, best-effort format | gemini-cli changes the event schema | file absent, no matching events | `unavailable` + the settings.json snippet to enable it |
| Gemini tier caps | hardcoded from public docs | Google changes tier quotas | undetectable — hence never `authoritative` | reported as `estimated` |

Both local-file fallbacks (`~/.claude/projects/*.jsonl`, `~/.gemini/telemetry.log`) are
undocumented-for-third-party-use internal formats owned by the respective CLIs. Every
`UsageWindow` carries a `confidence` field (`authoritative` / `estimated` / `unavailable`) and
a `source` string precisely so a consumer (human or script) can tell live-API data from
best-effort log scraping from "we don't know" — see `src/ai_usage_monitor/models.py`. `None`
means unknown and renders as `-`; it is never silently turned into `0` or a full bar.

Golden fixtures under `tests/fixtures/` hold today's real payload shapes with account-specific
values scrubbed, so a provider-side change shows up as a **failing test** rather than as a
silently wrong number in the terminal. Parsing is a pure `payload -> windows` function, so the
whole normalization layer is tested with no network and no keychain.

## Layout

- `providers/claude.py`, `providers/gemini.py` — one `fetch() -> ProviderSnapshot` per provider,
  each running its own ordered source chain.
- `credentials.py` — read-only keychain / credential-file access.
- `config.py` — optional `~/.config/ai-usage-monitor/config.toml`. Not needed for Claude's
  primary path; holds fallback caps and the `use_oauth_usage_api` / `credentials_file` knobs.
- `render.py` — shared rendering (rich panels + JSON) used by both CLI modes, so snapshot and
  dashboard never drift apart.
- `cli.py` — `snapshot` (default, supports `--json`) and `dashboard` (live, `rich.Live`)
  subcommands.
