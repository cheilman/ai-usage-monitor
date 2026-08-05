from ai_usage_monitor.providers.base import UsageProvider
from ai_usage_monitor.providers.claude import ClaudeProvider
from ai_usage_monitor.providers.gemini import GeminiProvider

ALL_PROVIDERS: dict[str, type[UsageProvider]] = {
    "claude": ClaudeProvider,
    "gemini": GeminiProvider,
}

__all__ = ["ALL_PROVIDERS", "ClaudeProvider", "GeminiProvider", "UsageProvider"]
