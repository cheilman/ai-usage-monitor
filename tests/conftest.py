import os

import pytest


@pytest.fixture(autouse=True)
def no_ambient_credentials(monkeypatch):
    """Keep the developer's own machine out of the tests.

    Nothing in the suite is allowed to touch the real keychain, a real API key, or the
    network -- every source is either disabled via config or monkeypatched per test.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CREDENTIALS_FILE", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    for name in [n for n in os.environ if n.startswith("OPENROUTER_API_KEY_")]:
        monkeypatch.delenv(name, raising=False)
