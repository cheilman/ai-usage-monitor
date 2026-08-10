import os

import pytest


@pytest.fixture(autouse=True)
def no_ambient_credentials(monkeypatch, tmp_path):
    """Keep the developer's own machine out of the tests.

    Nothing in the suite is allowed to touch the real keychain, a real API key, or the
    network -- every source is either disabled via config or monkeypatched per test. This
    includes the developer's own ~/.kiteng/.env: KITENG_HOME is pointed at an empty tmp dir
    so _discover_openrouter_keys' fallback never reads a real secret off this machine.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CREDENTIALS_FILE", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    for name in [n for n in os.environ if n.startswith("OPENROUTER_API_KEY_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KITENG_HOME", str(tmp_path))
