"""Turns ProviderSnapshot objects into rich renderables or plain JSON."""

from __future__ import annotations

from datetime import UTC, datetime

from rich.console import Group
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from ai_usage_monitor.models import Confidence, ProviderSnapshot, UsageWindow

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


def _format_countdown(reset_at: datetime | None, now: datetime) -> str:
    if reset_at is None:
        return "-"
    delta = reset_at - now
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "resetting..."
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
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

    return (
        window.label,
        f"{used_str} / {limit_str} {window.unit}",
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
                    complete_style=style,
                    finished_style="bold red",
                    width=None,
                )
                table.add_row(bar, "", "", "", "")
        parts.append(table)
    else:
        parts.append(Text("no data", style="dim"))

    for error in snapshot.errors:
        parts.append(Text(f"! {error}", style="italic dim"))

    return Panel(
        Group(*parts),
        title=f"[bold]{snapshot.provider.upper()}[/bold]",
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


def snapshot_to_dict(snapshot: ProviderSnapshot) -> dict:
    """Serialize a snapshot. Note this only ever emits usage numbers and labels -- API keys
    and OAuth tokens are not part of the model, so `--json` output and the on-disk cache
    (see cache.py) are both safe to write to a file or hand to another process."""
    return {
        "provider": snapshot.provider,
        "fetched_at": snapshot.fetched_at.isoformat(),
        "windows": [
            {
                "label": w.label,
                "unit": w.unit,
                "used": w.used,
                "limit": w.limit,
                "percent": w.percent,
                "reset_at": w.reset_at.isoformat() if w.reset_at else None,
                "confidence": w.confidence.value,
                "source": w.source,
                "note": w.note,
            }
            for w in snapshot.windows
        ],
        "errors": snapshot.errors,
    }


def _parse_iso(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


def snapshot_from_dict(payload: dict) -> ProviderSnapshot:
    """Inverse of `snapshot_to_dict`, for reading cached snapshots back.

    Deliberately strict: anything malformed raises (TypeError/ValueError/KeyError) rather than
    silently producing a half-empty snapshot, so the cache layer can treat it as a miss and
    fetch live instead. The derived `percent` field is recomputed, not read back.
    """
    fetched_at = _parse_iso(payload["fetched_at"])
    if fetched_at is None:
        raise ValueError("snapshot payload has no fetched_at")

    return ProviderSnapshot(
        provider=payload["provider"],
        fetched_at=fetched_at,
        windows=[
            UsageWindow(
                label=w["label"],
                unit=w["unit"],
                used=w["used"],
                limit=w["limit"],
                reset_at=_parse_iso(w["reset_at"]),
                confidence=Confidence(w["confidence"]),
                source=w["source"],
                note=w["note"],
            )
            for w in payload["windows"]
        ],
        errors=list(payload["errors"]),
    )
