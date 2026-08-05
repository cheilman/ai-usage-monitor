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
    # Provider-computed extras. Only sources that publish them (today: Claude's
    # /api/oauth/usage `limits[]`) fill these in; everything else leaves the defaults.
    is_active: bool = False
    severity: str | None = None

    @property
    def percent(self) -> float | None:
        if self.used is None or not self.limit:
            return None
        # Rounded so an already-normalized percentage (used=14, limit=100) round-trips as
        # 14.0 rather than 14.000000000000002 in --json output.
        return round(max(0.0, min(100.0, (self.used / self.limit) * 100.0)), 6)


@dataclass
class ProviderSnapshot:
    """Everything one provider has to report at a point in time."""

    provider: str
    fetched_at: datetime
    windows: list[UsageWindow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Plan identifier when the provider tells us one (e.g. "pro", "max"), else None.
    plan: str | None = None
    # Non-error side facts worth showing, e.g. extra-usage credits or period spend.
    notes: list[str] = field(default_factory=list)
