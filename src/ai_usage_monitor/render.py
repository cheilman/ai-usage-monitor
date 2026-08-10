"""Turns ProviderSnapshot objects into rich renderables or plain JSON."""

from __future__ import annotations

from datetime import UTC, datetime

from rich.console import Group
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from ai_usage_monitor.models import Confidence, ProviderSnapshot, UsageWindow

# Bumped only when the --json document shape changes incompatibly. Consumers should refuse
# to parse a schema_version they don't recognize rather than guess at the fields.
SCHEMA_VERSION = 1

# Tie-break for most_constrained: at equal utilization, trust the better-sourced number.
_CONFIDENCE_RANK = {
    Confidence.AUTHORITATIVE.value: 0,
    Confidence.ESTIMATED.value: 1,
    Confidence.UNAVAILABLE.value: 2,
}

_PROVIDER_STYLE = {
    "claude": "#d97757",
    "gemini": "#4285f4",
}

# Below this, "as of HH:MM:SS" is close enough to now that calling out the age is just noise.
_STALE_AFTER_SECONDS = 5

_CONFIDENCE_STYLE = {
    Confidence.AUTHORITATIVE: "bold green",
    Confidence.ESTIMATED: "yellow",
    Confidence.UNAVAILABLE: "dim red",
}

# Provider-reported severity, when the source publishes one (Claude's limits[]).
_SEVERITY_STYLE = {
    "normal": "green",
    "warning": "yellow",
    "critical": "bold red",
}


def _format_countdown(reset_at: datetime | None, now: datetime) -> str:
    if reset_at is None:
        return "-"
    delta = reset_at - now
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "resetting..."
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _window_row(window: UsageWindow, now: datetime) -> tuple[str, ...]:
    if window.used is None:
        used_str = "-"
    elif window.unit == "tokens" and window.used >= 1000:
        used_str = f"{window.used / 1000:.1f}k"
    else:
        used_str = f"{window.used:g}"

    if window.limit is None:
        limit_str = "?"
    elif window.unit == "tokens" and window.limit >= 1000:
        limit_str = f"{window.limit / 1000:.1f}k"
    else:
        limit_str = f"{window.limit:g}"

    pct = window.percent
    pct_str = f"{pct:.0f}%" if pct is not None else "-"

    # A percent-unit window is already fully described by the percentage column.
    detail = "" if window.unit == "percent" else f"{used_str} / {limit_str} {window.unit}"

    return (
        f"{window.label} *" if window.is_active else window.label,
        detail,
        pct_str,
        _format_countdown(window.reset_at, now),
        Text(window.confidence.value, style=_CONFIDENCE_STYLE[window.confidence]),
    )


def render_snapshot_panel(snapshot: ProviderSnapshot, now: datetime | None = None) -> Panel:
    now = now or datetime.now(UTC)
    style = _PROVIDER_STYLE.get(snapshot.provider, "white")

    table = Table.grid(padding=(0, 1))
    table.add_column(justify="left")
    table.add_column(justify="left")
    table.add_column(justify="right")
    table.add_column(justify="right")
    table.add_column(justify="left")

    parts: list = []
    if snapshot.windows:
        for window in snapshot.windows:
            table.add_row(*_window_row(window, now))
            pct = window.percent
            if pct is not None:
                bar = ProgressBar(
                    total=100,
                    completed=pct,
                    complete_style=_SEVERITY_STYLE.get(window.severity or "", style),
                    finished_style="bold red",
                    width=None,
                )
                table.add_row(bar, "", "", "", "")
        parts.append(table)
    else:
        parts.append(Text("no data", style="dim"))

    for note in snapshot.notes:
        parts.append(Text(note, style="dim"))

    for error in snapshot.errors:
        parts.append(Text(f"! {error}", style="italic dim"))

    title = f"[bold]{snapshot.provider.upper()}[/bold]"
    if snapshot.plan:
        title += f" [dim]plan: {snapshot.plan}[/dim]"

    return Panel(
        Group(*parts),
        title=title,
        border_style=style,
        subtitle=_subtitle(snapshot, now),
        subtitle_align="right",
    )


def _subtitle(snapshot: ProviderSnapshot, now: datetime) -> str:
    """When the *data* is from, not when we drew it -- with the cache in play those differ."""
    fetched = snapshot.fetched_at.astimezone(UTC)
    text = f"as of {fetched.strftime('%H:%M:%S UTC')}"
    age = int((now - fetched).total_seconds())
    if age >= _STALE_AFTER_SECONDS:
        text += f" ({age}s ago)"
    return text


def _window_dicts(snapshot: ProviderSnapshot) -> list[dict]:
    """Serialize one provider's windows, guaranteeing `key` is unique within the provider.

    Providers assign keys from a fixed table, but a payload could in principle report the
    same kind twice. Suffixing a repeat (`weekly_all_2`) keeps `key` usable as an index
    instead of silently giving a consumer two different rows under one name.
    """
    seen: dict[str, int] = {}
    dicts: list[dict] = []
    for w in snapshot.windows:
        count = seen.get(w.key, 0) + 1
        seen[w.key] = count
        dicts.append(
            {
                "key": w.key if count == 1 else f"{w.key}_{count}",
                "label": w.label,
                "unit": w.unit,
                "used": w.used,
                "limit": w.limit,
                "percent": w.percent,
                "reset_at": w.reset_at.isoformat() if w.reset_at else None,
                "confidence": w.confidence.value,
                "source": w.source,
                "note": w.note,
                "is_active": w.is_active,
                "severity": w.severity,
            }
        )
    return dicts


def snapshot_to_dict(snapshot: ProviderSnapshot) -> dict:
    """Serialize a snapshot. Note this only ever emits usage numbers and labels -- API keys
    and OAuth tokens are not part of the model, so `--json` output and the on-disk cache
    (see cache.py) are both safe to write to a file or hand to another process."""
    return {
        "provider": snapshot.provider,
        "status": snapshot.status.value,
        "fetched_at": snapshot.fetched_at.isoformat(),
        "plan": snapshot.plan,
        "windows": _window_dicts(snapshot),
        "notes": snapshot.notes,
        "errors": snapshot.errors,
    }


def _parse_iso(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


def snapshot_from_dict(payload: dict) -> ProviderSnapshot:
    """Inverse of `snapshot_to_dict`, for reading cached snapshots back.

    Deliberately strict: anything malformed raises (TypeError/ValueError/KeyError) rather than
    silently producing a half-empty snapshot, so the cache layer can treat it as a miss and
    fetch live instead. Derived fields (`percent`, `status`) are recomputed, not read back.
    """
    fetched_at = _parse_iso(payload["fetched_at"])
    if fetched_at is None:
        raise ValueError("snapshot payload has no fetched_at")

    return ProviderSnapshot(
        provider=payload["provider"],
        fetched_at=fetched_at,
        plan=payload.get("plan"),
        windows=[
            UsageWindow(
                key=w["key"],
                label=w["label"],
                unit=w["unit"],
                used=w["used"],
                limit=w["limit"],
                reset_at=_parse_iso(w["reset_at"]),
                confidence=Confidence(w["confidence"]),
                source=w["source"],
                note=w["note"],
                is_active=w.get("is_active", False),
                severity=w.get("severity"),
            )
            for w in payload["windows"]
        ],
        errors=list(payload["errors"]),
        notes=list(payload.get("notes", [])),
    )


def most_constrained(providers: list[dict]) -> dict | None:
    """The window closest to its cap across every provider, or None if nothing is known.

    This exists so the throttling consumer is a single lookup instead of a re-implementation
    of our precedence rules. Precedence: highest utilization wins; ties go to the better
    confidence, then to provider/key alphabetically so the answer is deterministic.

    `None` means **quota is unknown**, not "quota is available" -- a consumer must treat it
    as a reason to back off or to ask another way, never as a green light.

    Takes already-serialized provider dicts so the `key` it reports is exactly the one the
    caller can find under `providers[].windows[]`, suffixing and all.
    """
    candidates = [
        (provider["provider"], window)
        for provider in providers
        for window in provider["windows"]
        if window["percent"] is not None
        and window["confidence"] != Confidence.UNAVAILABLE.value
    ]
    if not candidates:
        return None

    provider_name, window = min(
        candidates,
        key=lambda c: (
            -c[1]["percent"],
            _CONFIDENCE_RANK.get(c[1]["confidence"], len(_CONFIDENCE_RANK)),
            c[0],
            c[1]["key"],
        ),
    )
    return {
        "provider": provider_name,
        "key": window["key"],
        "label": window["label"],
        "utilization_pct": window["percent"],
        "resets_at": window["reset_at"],
    }


def snapshots_to_document(
    snapshots: list[ProviderSnapshot], generated_at: datetime | None = None
) -> dict:
    """Build the versioned `--json` document (schema_version 1). The stable contract."""
    providers = [snapshot_to_dict(s) for s in snapshots]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": (generated_at or datetime.now(UTC)).isoformat(),
        "providers": providers,
        "most_constrained": most_constrained(providers),
    }
