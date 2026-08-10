from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from ai_usage_monitor.models import (
    ProviderSnapshot,
    SourceAttempt,
    SourceOutcome,
    UsageWindow,
)

# What a source function returns: the windows it produced (possibly empty) plus how it went.
SourceResult = tuple[list[UsageWindow], SourceOutcome, str, str | None]


class UsageProvider(Protocol):
    """Anything that can report a usage snapshot for one provider."""

    name: str

    def fetch(self) -> ProviderSnapshot: ...


def run_source(
    name: str,
    fn: Callable[[], SourceResult],
) -> tuple[list[UsageWindow], SourceAttempt]:
    """Run one source in a chain, timing it and recording a SourceAttempt either way.

    A source that raises is not allowed to take the whole snapshot down -- every source here
    reads undocumented files or unpublished endpoints, so an unexpected shape is a normal
    Tuesday. The exception becomes an `error` attempt and the chain continues.
    """
    started = time.perf_counter()
    try:
        windows, outcome, detail, remediation = fn()
    except Exception as exc:  # noqa: BLE001 - a broken source must not break the report
        windows = []
        outcome = SourceOutcome.ERROR
        detail = f"{type(exc).__name__}: {exc}"
        remediation = "Unexpected failure reading this source; the underlying format may have changed."

    duration_ms = (time.perf_counter() - started) * 1000.0
    return windows, SourceAttempt(
        name=name,
        outcome=outcome,
        detail=detail,
        duration_ms=duration_ms,
        remediation=remediation,
    )
