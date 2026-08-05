"""Turns ProviderSnapshot objects into rich renderables or plain JSON."""

from __future__ import annotations

from datetime import UTC, datetime

from rich.console import Group
from rich.padding import Padding
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from ai_usage_monitor.models import (
    Confidence,
    ProviderSnapshot,
    SourceAttempt,
    SourceOutcome,
    UsageWindow,
)

_PROVIDER_STYLE = {
    "claude": "#d97757",
    "gemini": "#4285f4",
}

_CONFIDENCE_STYLE = {
    Confidence.AUTHORITATIVE: "bold green",
    Confidence.ESTIMATED: "yellow",
    Confidence.UNAVAILABLE: "dim red",
}

_OUTCOME_STYLE = {
    SourceOutcome.OK: "bold green",
    SourceOutcome.EMPTY: "yellow",
    SourceOutcome.NOT_FOUND: "yellow",
    SourceOutcome.NO_CREDENTIAL: "dim",
    SourceOutcome.ERROR: "bold red",
}

_OUTCOME_MARK = {
    SourceOutcome.OK: "OK",
    SourceOutcome.EMPTY: "!",
    SourceOutcome.NOT_FOUND: "!",
    SourceOutcome.NO_CREDENTIAL: "-",
    SourceOutcome.ERROR: "X",
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

    # Partial degradation is the silent case: some sources worked, so there's no error, but a
    # source did break. When everything failed, the error above already points at doctor.
    failed = [a for a in snapshot.attempts if not a.ok]
    if failed and not snapshot.errors:
        parts.append(
            Text(
                f"{len(failed)}/{len(snapshot.attempts)} source(s) unavailable -- "
                f"run `ai-usage-monitor doctor --provider {snapshot.provider}`",
                style="italic dim",
            )
        )

    return Panel(
        Group(*parts),
        title=f"[bold]{snapshot.provider.upper()}[/bold]",
        border_style=style,
        subtitle=f"as of {now.strftime('%H:%M:%S UTC')}",
        subtitle_align="right",
    )


def _attempt_lines(attempt: SourceAttempt) -> list:
    mark = _OUTCOME_MARK[attempt.outcome]
    style = _OUTCOME_STYLE[attempt.outcome]

    head = Text()
    head.append(f"[{mark}] ", style=style)
    head.append(attempt.name, style="bold")
    head.append(f"  {attempt.outcome.value}", style=style)
    head.append(f"  ({attempt.duration_ms:.0f}ms)", style="dim")

    # Padding (rather than literal spaces) so wrapped continuation lines stay indented too.
    lines = [head, Padding(Text(attempt.detail, style="dim"), (0, 0, 0, 6))]
    if attempt.remediation:
        lines.append(Padding(Text(f"-> {attempt.remediation}", style="cyan"), (0, 0, 0, 6)))
    return lines


def render_doctor_panel(snapshot: ProviderSnapshot) -> Panel:
    """Per-source diagnostic report: what we tried, how it went, what to do about it."""
    style = _PROVIDER_STYLE.get(snapshot.provider, "white")

    parts: list = []
    if snapshot.attempts:
        for index, attempt in enumerate(snapshot.attempts):
            if index:
                parts.append(Text(""))
            parts.extend(_attempt_lines(attempt))
    else:
        parts.append(Text("no sources were attempted", style="dim"))

    ok_count = sum(1 for a in snapshot.attempts if a.ok)
    parts.append(Text(""))
    parts.append(
        Text(
            f"{ok_count}/{len(snapshot.attempts)} source(s) healthy; overall status: "
            f"{snapshot.status.value}",
            style=_CONFIDENCE_STYLE[snapshot.status],
        )
    )

    return Panel(
        Group(*parts),
        title=f"[bold]{snapshot.provider.upper()}[/bold] sources",
        border_style=style,
        title_align="left",
    )


def attempt_to_dict(attempt: SourceAttempt) -> dict:
    return {
        "name": attempt.name,
        "outcome": attempt.outcome.value,
        "detail": attempt.detail,
        "duration_ms": round(attempt.duration_ms, 3),
        "remediation": attempt.remediation,
    }


def snapshot_to_dict(snapshot: ProviderSnapshot) -> dict:
    return {
        "provider": snapshot.provider,
        "fetched_at": snapshot.fetched_at.isoformat(),
        "status": snapshot.status.value,
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
        "attempts": [attempt_to_dict(a) for a in snapshot.attempts],
        "errors": snapshot.errors,
    }
