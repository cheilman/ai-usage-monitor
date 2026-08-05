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
        subtitle=f"as of {now.strftime('%H:%M:%S UTC')}",
        subtitle_align="right",
    )


def snapshot_to_dict(snapshot: ProviderSnapshot) -> dict:
    return {
        "provider": snapshot.provider,
        "fetched_at": snapshot.fetched_at.isoformat(),
        "plan": snapshot.plan,
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
                "is_active": w.is_active,
                "severity": w.severity,
            }
            for w in snapshot.windows
        ],
        "notes": snapshot.notes,
        "errors": snapshot.errors,
    }
