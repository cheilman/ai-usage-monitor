"""Shared data model for provider usage snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Confidence(str, Enum):
    """How much to trust a given usage number."""

    # Read straight from a provider-authored, live API response (e.g. rate-limit headers).
    AUTHORITATIVE = "authoritative"
    # Derived locally (log scraping, hardcoded public tier docs) and may drift from reality.
    ESTIMATED = "estimated"
    # We couldn't find any signal at all.
    UNAVAILABLE = "unavailable"


class SourceOutcome(str, Enum):
    """Result of trying one source in a provider's ordered chain."""

    # Source produced usable numbers.
    OK = "ok"
    # Source exists and was readable, but had nothing to report (e.g. empty log).
    EMPTY = "empty"
    # Source isn't there at all (file/dir missing).
    NOT_FOUND = "not_found"
    # Source exists but we have no credential for it.
    NO_CREDENTIAL = "no_credential"
    # Source was reachable but rejected/failed us (HTTP error, bad schema, unreadable file).
    ERROR = "error"


@dataclass
class SourceAttempt:
    """One entry in the audit trail of a provider's source chain.

    Every source a provider tries records one of these, whether it worked or not. This is
    what `ai-usage-monitor doctor` prints: without it, an undocumented source that quietly
    stops working just looks like "the tool prints nothing".
    """

    name: str
    outcome: SourceOutcome
    detail: str
    duration_ms: float
    # What the user could do about it, when the outcome is actionable.
    remediation: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is SourceOutcome.OK


@dataclass
class UsageWindow:
    """One usage/limit pair, e.g. "5-hour session tokens" or "requests per minute"."""

    label: str
    unit: str
    used: float | None
    limit: float | None
    reset_at: datetime | None
    confidence: Confidence
    source: str
    note: str | None = None

    @property
    def percent(self) -> float | None:
        if self.used is None or not self.limit:
            return None
        return max(0.0, min(100.0, (self.used / self.limit) * 100.0))


@dataclass
class ProviderSnapshot:
    """Everything one provider has to report at a point in time."""

    provider: str
    fetched_at: datetime
    windows: list[UsageWindow] = field(default_factory=list)
    # Every source tried, in the order it was tried, successful or not.
    attempts: list[SourceAttempt] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def status(self) -> Confidence:
        """Best confidence across windows that actually carry a number."""
        with_data = [w for w in self.windows if w.used is not None]
        if any(w.confidence is Confidence.AUTHORITATIVE for w in with_data):
            return Confidence.AUTHORITATIVE
        if any(w.confidence is Confidence.ESTIMATED for w in with_data):
            return Confidence.ESTIMATED
        return Confidence.UNAVAILABLE
