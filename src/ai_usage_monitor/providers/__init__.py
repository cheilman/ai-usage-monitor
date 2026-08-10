from ai_usage_monitor.providers.base import UsageProvider
from ai_usage_monitor.providers.claude import ClaudeProvider
from ai_usage_monitor.providers.gemini import GeminiProvider
from ai_usage_monitor.providers.openrouter import OpenRouterProvider

ALL_PROVIDERS: dict[str, type[UsageProvider]] = {
    "claude": ClaudeProvider,
    "gemini": GeminiProvider,
    "openrouter": OpenRouterProvider,
}

__all__ = [
    "ALL_PROVIDERS",
    "ClaudeProvider",
    "GeminiProvider",
    "OpenRouterProvider",
    "UsageProvider",
]
