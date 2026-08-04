from __future__ import annotations

from typing import Protocol

from ai_usage_monitor.models import ProviderSnapshot


class UsageProvider(Protocol):
    """Anything that can report a usage snapshot for one provider."""

    name: str

    def fetch(self) -> ProviderSnapshot: ...
