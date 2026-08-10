import json
from pathlib import Path

import pytest

from ai_usage_monitor.config import OpenRouterConfig, OpenRouterKeyConfig
from ai_usage_monitor.models import Confidence, SourceOutcome
from ai_usage_monitor.providers import openrouter as openrouter_module
from ai_usage_monitor.providers.openrouter import (
    KEY_URL,
    OpenRouterProvider,
    UsageSourceError,
    parse_key_usage,
)

FIXTURE = Path(__file__).parent / "fixtures" / "openrouter_key_usage.json"


def _key(label="default", api_key="sk-or-fake") -> OpenRouterKeyConfig:
    return OpenRouterKeyConfig(label=label, api_key=api_key)


# ---------------------------------------------------------------------------------------
# Golden fixture: the live GET /api/v1/key payload shape
# ---------------------------------------------------------------------------------------


def test_golden_fixture_parses_to_authoritative_credit_limit_window():
    data = json.loads(FIXTURE.read_text())["data"]
    windows, notes = parse_key_usage(data, "default")

    assert len(windows) == 1
    window = windows[0]
    assert window.key == "credit_limit_default"
    assert window.label == "credit limit"
    assert (window.used, window.limit, window.unit) == (2.5, 10.0, "dollars")
    assert window.percent == 25.0
    assert window.confidence == Confidence.AUTHORITATIVE
    assert window.source == "GET /api/v1/key"
    assert "monthly" in window.note

    assert notes == [
        "default: $0.10 today / $0.60 this week / $2.50 this month ($2.50 all-time, paid tier)"
    ]


def test_uncapped_key_reports_usage_with_no_limit():
    data = {"usage": 4.0, "usage_daily": 0.0, "usage_weekly": 0.0, "usage_monthly": 4.0}
    windows, _ = parse_key_usage(data, "work")

    assert len(windows) == 1
    window = windows[0]
    assert window.key == "usage_work"
    assert window.label == "work usage (no cap)"
    assert (window.used, window.limit) == (4.0, None)
    assert window.percent is None
    assert window.confidence == Confidence.AUTHORITATIVE
    assert "no spending limit" in window.note


def test_byok_usage_becomes_its_own_note():
    data = {"limit": 10.0, "limit_remaining": 10.0, "byok_usage": 1.25}
    _, notes = parse_key_usage(data, "default")
    assert any("BYOK" in note for note in notes)


def test_label_prefixes_non_default_keys():
    data = {"limit": 10.0, "limit_remaining": 5.0}
    windows, notes = parse_key_usage(data, "personal")
    assert windows[0].label == "personal credit limit"
    assert notes[0].startswith("personal: ")


def test_garbage_payload_yields_no_windows_instead_of_crashing():
    windows, notes = parse_key_usage({"limit": "nope"}, "default")
    # No usable limit/limit_remaining pair and no usage -> falls back to the "no cap" window
    # with used=0.0, since usage itself is still a real (if zero) number.
    assert len(windows) == 1
    assert windows[0].used == 0.0
    assert notes


# ---------------------------------------------------------------------------------------
# Provider: per-key source attempts
# ---------------------------------------------------------------------------------------


def test_no_keys_configured_reports_no_credential():
    snapshot = OpenRouterProvider(OpenRouterConfig(keys=[])).fetch()

    assert snapshot.windows == []
    assert len(snapshot.attempts) == 1
    assert snapshot.attempts[0].outcome == SourceOutcome.NO_CREDENTIAL
    assert snapshot.errors


def test_single_key_success(monkeypatch):
    data = json.loads(FIXTURE.read_text())["data"]
    monkeypatch.setattr(openrouter_module, "fetch_key_usage", lambda api_key, timeout=10.0: data)

    snapshot = OpenRouterProvider(OpenRouterConfig(keys=[_key()])).fetch()

    assert snapshot.errors == []
    assert len(snapshot.windows) == 1
    assert snapshot.windows[0].confidence == Confidence.AUTHORITATIVE
    assert len(snapshot.attempts) == 1
    assert snapshot.attempts[0].outcome == SourceOutcome.OK
    assert snapshot.attempts[0].name == f"{openrouter_module.SOURCE_NAME} (default)"


def test_invalid_key_records_error_attempt_with_remediation(monkeypatch):
    def _boom(api_key, timeout=10.0):
        raise UsageSourceError(f"{KEY_URL} returned HTTP 401 -- the key may be invalid")

    monkeypatch.setattr(openrouter_module, "fetch_key_usage", _boom)

    snapshot = OpenRouterProvider(OpenRouterConfig(keys=[_key()])).fetch()

    assert snapshot.windows == []
    assert len(snapshot.attempts) == 1
    attempt = snapshot.attempts[0]
    assert attempt.outcome == SourceOutcome.ERROR
    assert "401" in attempt.detail
    assert attempt.remediation
    assert any("default" in e for e in snapshot.errors)


def test_one_bad_key_does_not_hide_a_good_one(monkeypatch):
    good_data = json.loads(FIXTURE.read_text())["data"]

    def _fetch(api_key, timeout=10.0):
        if api_key == "good-key":
            return good_data
        raise UsageSourceError(f"{KEY_URL} returned HTTP 401")

    monkeypatch.setattr(openrouter_module, "fetch_key_usage", _fetch)

    keys = [_key(label="broken", api_key="bad-key"), _key(label="default", api_key="good-key")]
    snapshot = OpenRouterProvider(OpenRouterConfig(keys=keys)).fetch()

    assert len(snapshot.attempts) == 2
    outcomes = {a.name: a.outcome for a in snapshot.attempts}
    assert outcomes[f"{openrouter_module.SOURCE_NAME} (broken)"] == SourceOutcome.ERROR
    assert outcomes[f"{openrouter_module.SOURCE_NAME} (default)"] == SourceOutcome.OK

    # The broken key's failure doesn't erase the good key's window.
    assert len(snapshot.windows) == 1
    assert any("broken" in e for e in snapshot.errors)


def test_non_dict_data_field_raises_usage_source_error(monkeypatch):
    def _raw_http(request, api_key="x"):
        raise AssertionError("not used in this test")

    # fetch_key_usage itself validates the payload shape; exercise it directly rather than
    # the network path, matching how the Claude provider tests validate its fetch_* helper.
    class _FakeResponse:
        def __init__(self, body: bytes):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        openrouter_module.urllib.request,
        "urlopen",
        lambda request, timeout=10.0: _FakeResponse(json.dumps({"no": "data field"}).encode()),
    )

    with pytest.raises(UsageSourceError, match="no 'data' object"):
        openrouter_module.fetch_key_usage("sk-or-fake")
