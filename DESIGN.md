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

**OpenRouter** — chain: `GET /api/v1/key` → unavailable. No fallback exists because none is
needed.

- Unlike Claude and Gemini, OpenRouter publishes a small, documented, key-scoped usage
  endpoint: `GET https://openrouter.ai/api/v1/key`, `Authorization: Bearer <api key>` (see
  https://openrouter.ai/docs/api_reference/limits). It returns the spending cap configured on
  *that key* (`limit` / `limit_remaining` / `limit_reset`, `null` when uncapped) plus running
  totals (`usage`, `usage_daily`, `usage_weekly`, `usage_monthly`) and the same figures for
  BYOK (bring-your-own-key) spend. Every window from it is `authoritative`.
- "Usage by API key(s)" (plural) is the point: a user may hold several keys (personal,
  project, CI) and wants all of them at a glance. There is no config-file knob for the secret
  itself — `config._discover_openrouter_keys` finds every `OPENROUTER_API_KEY` (→ label
  `default`) and `OPENROUTER_API_KEY_<LABEL>` (→ label `<label>`) environment variable, the
  same rule Claude's `ANTHROPIC_API_KEY` follows.
- Each key is queried and reported independently, with its own `SourceAttempt` named after its
  label, so a revoked or misconfigured key can't hide the others — `doctor` says exactly which
  key failed and why. No keys configured at all reports `no_credential` with the `export`
  needed, rather than silently omitting the provider.
- `_discover_openrouter_keys` also falls back to `$KITENG_HOME/.env` (default `~/.kiteng/.env`)
  for any `OPENROUTER_API_KEY*` name not already in `os.environ` — the same file kiteng's own
  `load_env_file()` reads to provision secrets for agent subprocesses (launchd/cron, no shell
  `export` in scope). It's a fallback only: a name already set in the process environment is
  never overridden by the file, matching that file's own documented purpose. A missing or
  unreadable file just means no fallback values, never an error.

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
| OpenRouter `/api/v1/key` | documented, but small and could still change | OpenRouter renames/removes a field | HTTP != 200, non-JSON body, no `data` object, no recognizable usage fields | `unavailable` for that key; other keys unaffected |

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

## The `--json` contract (schema_version 1)

Everything above is about *getting* numbers we can defend. This section is about handing them
to a machine — specifically the KitEng throttling consumer, which decides whether to start
work based on how much quota is left. That consumer is why `--json` is a versioned envelope
rather than whatever the renderer happened to produce:

```json
{ "schema_version": 1, "generated_at": "...", "providers": [...], "most_constrained": {...} }
```

- **`schema_version`** — a top-level integer a consumer can gate on. Compatible additions
  don't move it; anything that would break a parser does. Consumers should refuse an
  unrecognized version rather than guess.
- **`generated_at`** — one document-level timestamp, distinct from each provider's own
  `fetched_at`, so a cached or piped document can be aged without inspecting its contents.
- **`most_constrained`** — the whole point. The precedence rules (which window matters,
  what beats what at equal utilization, which windows aren't trustworthy enough to count)
  live here, computed once, so the consumer is a one-line lookup instead of a second, drifting
  implementation of our own semantics. Highest utilization wins; ties break on `confidence`,
  then provider/key alphabetically for determinism.

  It is `null` exactly when no window anywhere has a known, trustworthy utilization. **`null`
  means quota is unknown, not available** — the same rule the rest of this document applies to
  `None`. A consumer that reads `null` as "plenty left" inverts the one guarantee this design
  is built around, so it's stated in the schema docs, enforced by the contract test, and
  reinforced by the exit code below.

**`key` vs `label`.** Every window carries a stable `key` (`five_hour`, `weekly_all`, ...)
separate from its display `label`. Without it, a consumer has to match on prose — and prose is
exactly what we reword when a provider renames something or a panel gets too wide. Keys are
unique within a provider (a collision gets suffixed rather than silently shadowing a row), and
an upstream kind we don't recognize gets a slugified key rather than being dropped. Note the
transcript fallback deliberately does *not* reuse `five_hour`/`weekly_all`: it measures raw
tokens against a hand-configured cap, not plan utilization, and a consumer that only
understands the authoritative keys should see it as absent rather than as a lower-quality
impostor wearing the same name.

**Exit codes** carry the same signal for callers that don't parse at all. Each provider rolls
up to `ok` / `degraded` / `unavailable` (authoritative-and-no-failures / fallback-or-estimate /
nothing usable). Exit `0` means at least one provider is `ok`; exit `2` means none is. A
recorded error demotes a provider even if some window is authoritative — otherwise a Claude
run whose subscription lookup failed would still report `ok` on the strength of an unrelated
API-key rate-limit header. `--fail-on-degraded` raises the bar to *every* provider being `ok`,
for callers that would rather stop than act on an estimate. The net effect is that
`ai-usage-monitor --json || back_off` is a correct gate by itself: the failure mode of the
whole tool is "refuse to say", and that refusal is visible without reading a byte of output.

`tests/test_json_contract.py` validates the emitted document against this shape field by
field, including that `most_constrained` really is the maximum and really does resolve back to
a window in `providers[]`. The contract erodes loudly or not at all.

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

- `providers/claude.py`, `providers/gemini.py`, `providers/openrouter.py` — one
  `fetch() -> ProviderSnapshot` per provider, each running its own ordered source chain through
  `run_source` (OpenRouter's "chain" is one source per configured key).
- `providers/base.py` — the `UsageProvider` protocol and `run_source`, which times one source
  and turns its result (or its exception) into a `SourceAttempt`.
- `credentials.py` — read-only keychain / credential-file access.
- `cache.py` — TTL'd on-disk snapshot cache plus `fetch_snapshots()`, the single read-through
  entry point both CLI modes use.
- `config.py` — optional `~/.config/ai-usage-monitor/config.toml`. Not needed for Claude's
  primary path; holds fallback caps and the `use_oauth_usage_api` / `credentials_file` knobs.
- `render.py` — shared rendering (rich panels + JSON) and the snapshot (de)serializer used by
  all CLI modes and the cache, so snapshot, dashboard, and doctor never drift apart. Also owns
  the v1 document: `snapshots_to_document()` and the `most_constrained()` precedence rules.
- `cli.py` — `snapshot` (default, supports `--json` / `--fail-on-degraded`), `doctor`
  (per-source diagnostics), and `dashboard` (live, `rich.Live`) subcommands, plus `exit_code()`.
