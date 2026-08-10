from ai_usage_monitor.config import _discover_openrouter_keys, _parse_env_file


def test_no_env_vars_yields_no_keys(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert _discover_openrouter_keys() == []


def test_default_key_from_bare_env_var(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-default")
    keys = _discover_openrouter_keys()
    assert [(k.label, k.api_key) for k in keys] == [("default", "sk-or-default")]


def test_labelled_keys_are_discovered_and_sorted(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY_WORK", "sk-or-work")
    monkeypatch.setenv("OPENROUTER_API_KEY_CI_BOT", "sk-or-ci")

    keys = _discover_openrouter_keys()

    assert [(k.label, k.api_key) for k in keys] == [
        ("ci-bot", "sk-or-ci"),
        ("work", "sk-or-work"),
    ]


def test_default_key_is_reported_before_labelled_keys(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-default")
    monkeypatch.setenv("OPENROUTER_API_KEY_WORK", "sk-or-work")

    keys = _discover_openrouter_keys()

    assert [k.label for k in keys] == ["default", "work"]


def test_empty_env_var_value_is_ignored(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("OPENROUTER_API_KEY_WORK", "")
    assert _discover_openrouter_keys() == []


def test_parse_env_file_handles_comments_blanks_and_quotes():
    text = """
# a comment
OPENROUTER_API_KEY="sk-or-fake-quoted"

OPENROUTER_API_KEY_WORK='sk-or-fake-single-quoted'
NOT_RELEVANT=other-value
"""
    assert _parse_env_file(text) == {
        "OPENROUTER_API_KEY": "sk-or-fake-quoted",
        "OPENROUTER_API_KEY_WORK": "sk-or-fake-single-quoted",
        "NOT_RELEVANT": "other-value",
    }


def test_key_falls_back_to_kiteng_env_file(monkeypatch, tmp_path):
    # no_ambient_credentials (conftest) already pointed KITENG_HOME at this tmp_path.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text('OPENROUTER_API_KEY="sk-or-fake-from-dotenv"\n')

    keys = _discover_openrouter_keys()

    assert [(k.label, k.api_key) for k in keys] == [("default", "sk-or-fake-from-dotenv")]


def test_process_env_var_takes_precedence_over_kiteng_env_file(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake-from-shell")
    (tmp_path / ".env").write_text('OPENROUTER_API_KEY="sk-or-fake-from-dotenv"\n')

    keys = _discover_openrouter_keys()

    assert [(k.label, k.api_key) for k in keys] == [("default", "sk-or-fake-from-shell")]


def test_missing_kiteng_env_file_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert not (tmp_path / ".env").exists()
    assert _discover_openrouter_keys() == []
