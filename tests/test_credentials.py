import json
from datetime import UTC, datetime, timedelta

import pytest

from ai_usage_monitor import credentials
from ai_usage_monitor.credentials import CredentialError, load_claude_credential


def _blob(**oauth) -> str:
    base = {
        "accessToken": "sk-ant-oat-fake",
        "refreshToken": "sk-ant-ort-fake",
        "expiresAt": int((datetime.now(UTC) + timedelta(hours=8)).timestamp() * 1000),
        "scopes": ["user:inference"],
        "subscriptionType": "pro",
    }
    base.update(oauth)
    return json.dumps({"claudeAiOauth": base})


def test_reads_credential_file_and_parses_epoch_millis(tmp_path, monkeypatch):
    monkeypatch.setattr(
        credentials, "_read_keychain", lambda *a, **k: pytest.fail("keychain must not be consulted")
    )
    path = tmp_path / ".credentials.json"
    path.write_text(_blob())

    credential = load_claude_credential(path)
    assert credential.access_token == "sk-ant-oat-fake"
    assert credential.subscription_type == "pro"
    assert credential.expires_at is not None
    assert credential.expires_at.tzinfo is not None
    assert not credential.is_expired


def test_expired_token_is_flagged(tmp_path):
    path = tmp_path / ".credentials.json"
    past = int((datetime.now(UTC) - timedelta(hours=1)).timestamp() * 1000)
    path.write_text(_blob(expiresAt=past))

    assert load_claude_credential(path).is_expired


def test_env_override_takes_precedence_over_keychain(tmp_path, monkeypatch):
    monkeypatch.setattr(
        credentials, "_read_keychain", lambda *a, **k: pytest.fail("keychain must not be consulted")
    )
    path = tmp_path / "from-env.json"
    path.write_text(_blob())
    monkeypatch.setenv("CLAUDE_CREDENTIALS_FILE", str(path))

    assert load_claude_credential().access_token == "sk-ant-oat-fake"


def test_missing_explicit_file_raises_without_leaking_secrets(tmp_path):
    missing = tmp_path / "nope.json"
    with pytest.raises(CredentialError) as exc:
        load_claude_credential(missing)
    assert "does not exist" in str(exc.value)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("not json", "not valid JSON"),
        ('{"other": {}}', "no 'claudeAiOauth' section"),
        ('{"claudeAiOauth": {"refreshToken": "x"}}', "no 'accessToken'"),
    ],
)
def test_schema_drift_produces_a_clear_error(tmp_path, raw, expected):
    path = tmp_path / ".credentials.json"
    path.write_text(raw)
    with pytest.raises(CredentialError) as exc:
        load_claude_credential(path)
    assert expected in str(exc.value)


def test_missing_everything_names_where_it_looked(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "_read_keychain", lambda *a, **k: None)
    monkeypatch.setattr(credentials, "DEFAULT_CREDENTIALS_FILE", tmp_path / "absent.json")

    with pytest.raises(CredentialError) as exc:
        load_claude_credential()
    assert "no Claude Code OAuth credential found" in str(exc.value)
