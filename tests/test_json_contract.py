"""Contract test for the `--json` document (schema_version 1).

This is the interface the KitEng throttling consumer keys off, so it gets a test that
validates the *shape* independently of any provider's internals. `assert_valid_v1` is a
standalone validator: if a future change drops `most_constrained`, renames a window field,
or lets a key collide, this fails loudly instead of the contract eroding one PR at a time.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from ai_usage_monitor.cli import EXIT_NO_USABLE_DATA, EXIT_OK, exit_code
from ai_usage_monitor.models import (
    Confidence,
    ProviderSnapshot,
    ProviderStatus,
    UsageWindow,
)
from ai_usage_monitor.render import SCHEMA_VERSION, most_constrained, snapshots_to_document

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

# Every window field a v1 consumer is allowed to rely on, and the types it may see.
WINDOW_FIELDS = {
    "key": str,
    "label": str,
    "unit": str,
    "used": (float, int, type(None)),
    "limit": (float, int, type(None)),
    "percent": (float, int, type(None)),
    "reset_at": (str, type(None)),
    "confidence": str,
    "source": str,
    "note": (str, type(None)),
    "is_active": bool,
    "severity": (str, type(None)),
}

PROVIDER_FIELDS = {
    "provider": str,
    "status": str,
    "fetched_at": str,
    "plan": (str, type(None)),
    "windows": list,
    "notes": list,
    "errors": list,
}

MOST_CONSTRAINED_FIELDS = {
    "provider": str,
    "key": str,
    "label": str,
    "utilization_pct": (float, int),
    "resets_at": (str, type(None)),
}


def assert_valid_v1(document: dict) -> None:
    """Assert `document` satisfies the schema_version 1 contract, end to end."""
    assert set(document) == {"schema_version", "generated_at", "providers", "most_constrained"}
    assert document["schema_version"] == 1
    datetime.fromisoformat(document["generated_at"])  # parseable ISO-8601

    assert isinstance(document["providers"], list)
    for provider in document["providers"]:
        assert set(provider) == set(PROVIDER_FIELDS)
        for field, expected in PROVIDER_FIELDS.items():
            assert isinstance(provider[field], expected), f"{provider['provider']}.{field}"
        assert provider["status"] in {s.value for s in ProviderStatus}
        datetime.fromisoformat(provider["fetched_at"])
        assert all(isinstance(n, str) for n in provider["notes"] + provider["errors"])

        keys = [w["key"] for w in provider["windows"]]
        assert len(keys) == len(set(keys)), f"duplicate window keys in {provider['provider']}"
        for window in provider["windows"]:
            assert set(window) == set(WINDOW_FIELDS)
            for field, expected in WINDOW_FIELDS.items():
                assert isinstance(window[field], expected), f"{window['key']}.{field}"
            assert window["confidence"] in {c.value for c in Confidence}
            if window["reset_at"] is not None:
                datetime.fromisoformat(window["reset_at"])
            if window["percent"] is not None:
                assert 0.0 <= window["percent"] <= 100.0

    winner = document["most_constrained"]
    if winner is None:
        # Contract: null is only legal when *nothing* is known. It must never be used to
        # paper over a window we did in fact measure.
        assert not [
            w
            for p in document["providers"]
            for w in p["windows"]
            if w["percent"] is not None and w["confidence"] != Confidence.UNAVAILABLE.value
        ]
        return

    assert set(winner) == set(MOST_CONSTRAINED_FIELDS)
    for field, expected in MOST_CONSTRAINED_FIELDS.items():
        assert isinstance(winner[field], expected), f"most_constrained.{field}"

    # The winner must resolve back to a real window, and must actually be the maximum.
    provider = next(p for p in document["providers"] if p["provider"] == winner["provider"])
    window = next(w for w in provider["windows"] if w["key"] == winner["key"])
    assert window["percent"] == winner["utilization_pct"]
    assert window["reset_at"] == winner["resets_at"]
    assert window["label"] == winner["label"]
    known = [
        w["percent"]
        for p in document["providers"]
        for w in p["windows"]
        if w["percent"] is not None and w["confidence"] != Confidence.UNAVAILABLE.value
    ]
    assert winner["utilization_pct"] == max(known)


def window(key, percent, *, confidence=Confidence.AUTHORITATIVE, reset_at=None, **kwargs):
    return UsageWindow(
        key=key,
        label=key.replace("_", " ").title(),
        unit="percent",
        used=percent,
        limit=100.0 if percent is not None else None,
        reset_at=reset_at,
        confidence=confidence,
        source="test",
        **kwargs,
    )


def snapshot(provider, windows, **kwargs):
    return ProviderSnapshot(provider=provider, fetched_at=NOW, windows=windows, **kwargs)


# ---------------------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------------------


def test_document_is_an_envelope_not_a_bare_array():
    document = snapshots_to_document([snapshot("claude", [window("five_hour", 21.0)])], NOW)

    assert isinstance(document, dict)
    assert document["schema_version"] == SCHEMA_VERSION == 1
    assert document["generated_at"] == NOW.isoformat()
    assert [p["provider"] for p in document["providers"]] == ["claude"]
    assert_valid_v1(document)


def test_document_round_trips_through_json():
    document = snapshots_to_document(
        [
            snapshot("claude", [window("five_hour", 21.0, reset_at=NOW + timedelta(hours=3))]),
            snapshot("gemini", [window("daily_requests", 4.0, confidence=Confidence.ESTIMATED)]),
        ],
        NOW,
    )
    assert_valid_v1(json.loads(json.dumps(document)))


def test_generated_at_defaults_to_now():
    before = datetime.now(UTC)
    document = snapshots_to_document([snapshot("claude", [])])
    assert before <= datetime.fromisoformat(document["generated_at"]) <= datetime.now(UTC)


# ---------------------------------------------------------------------------------------
# most_constrained
# ---------------------------------------------------------------------------------------


def test_most_constrained_picks_the_highest_utilization_across_providers():
    reset = NOW + timedelta(days=2)
    document = snapshots_to_document(
        [
            snapshot(
                "claude",
                [window("five_hour", 21.0), window("weekly_all", 54.0, reset_at=reset)],
            ),
            snapshot("gemini", [window("daily_requests", 30.0)]),
        ],
        NOW,
    )

    assert document["most_constrained"] == {
        "provider": "claude",
        "key": "weekly_all",
        "label": "Weekly All",
        "utilization_pct": 54.0,
        "resets_at": reset.isoformat(),
    }
    assert_valid_v1(document)


def test_most_constrained_is_null_when_nothing_is_known():
    """null means *unknown*, and a consumer must not read it as 'quota available'."""
    document = snapshots_to_document(
        [
            snapshot("claude", [], errors=["live usage API unavailable: no credential"]),
            snapshot(
                "gemini",
                [window("daily_requests", None, confidence=Confidence.UNAVAILABLE)],
            ),
        ],
        NOW,
    )

    assert document["most_constrained"] is None
    assert_valid_v1(document)


def test_most_constrained_ignores_unavailable_windows():
    document = snapshots_to_document(
        [
            snapshot(
                "claude",
                [
                    window("five_hour", 10.0),
                    # An unavailable window's number isn't trustworthy even when present.
                    window("weekly_tokens", 99.0, confidence=Confidence.UNAVAILABLE),
                ],
            )
        ],
        NOW,
    )

    assert document["most_constrained"]["key"] == "five_hour"


def test_most_constrained_breaks_ties_by_confidence_then_alphabetically():
    tied = most_constrained(
        [
            {
                "provider": "gemini",
                "windows": [
                    {
                        "key": "daily_requests",
                        "label": "x",
                        "percent": 50.0,
                        "reset_at": None,
                        "confidence": "authoritative",
                    }
                ],
            },
            {
                "provider": "claude",
                "windows": [
                    {
                        "key": "five_hour",
                        "label": "y",
                        "percent": 50.0,
                        "reset_at": None,
                        "confidence": "estimated",
                    }
                ],
            },
        ]
    )
    # Equal utilization: the authoritative one wins even though "claude" sorts first.
    assert (tied["provider"], tied["key"]) == ("gemini", "daily_requests")


def test_most_constrained_of_no_providers_is_null():
    assert most_constrained([]) is None
    assert snapshots_to_document([], NOW)["most_constrained"] is None


# ---------------------------------------------------------------------------------------
# Stable keys
# ---------------------------------------------------------------------------------------


def test_window_key_is_distinct_from_the_display_label():
    document = snapshots_to_document([snapshot("claude", [window("five_hour", 1.0)])], NOW)
    emitted = document["providers"][0]["windows"][0]
    assert emitted["key"] == "five_hour"
    assert emitted["label"] == "Five Hour"


def test_duplicate_window_keys_are_suffixed_so_key_stays_an_index():
    document = snapshots_to_document(
        [snapshot("claude", [window("weekly_all", 10.0), window("weekly_all", 20.0)])], NOW
    )

    assert [w["key"] for w in document["providers"][0]["windows"]] == [
        "weekly_all",
        "weekly_all_2",
    ]
    assert document["most_constrained"]["key"] == "weekly_all_2"
    assert_valid_v1(document)


# ---------------------------------------------------------------------------------------
# Provider status + exit codes (design §5.5)
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("snap", "expected"),
    [
        (snapshot("claude", [window("five_hour", 21.0)]), ProviderStatus.OK),
        (snapshot("claude", []), ProviderStatus.UNAVAILABLE),
        (
            snapshot("gemini", [window("daily_requests", None, confidence=Confidence.UNAVAILABLE)]),
            ProviderStatus.UNAVAILABLE,
        ),
        (
            snapshot("gemini", [window("daily_requests", 5.0, confidence=Confidence.ESTIMATED)]),
            ProviderStatus.DEGRADED,
        ),
        # Authoritative, but something in the chain failed -- the number is a fallback.
        (
            snapshot("claude", [window("api_tokens_per_minute", 3.0)], errors=["oauth failed"]),
            ProviderStatus.DEGRADED,
        ),
    ],
)
def test_provider_status(snap, expected):
    assert snap.status is expected


def test_exit_code_is_ok_when_any_provider_is_ok():
    snapshots = [
        snapshot("claude", [window("five_hour", 21.0)]),
        snapshot("gemini", [window("daily_requests", None, confidence=Confidence.UNAVAILABLE)]),
    ]
    assert exit_code(snapshots) == EXIT_OK


def test_exit_code_is_two_when_every_provider_is_unusable():
    snapshots = [
        snapshot("claude", [], errors=["live usage API unavailable"]),
        snapshot("gemini", [window("daily_requests", None, confidence=Confidence.UNAVAILABLE)]),
    ]
    assert exit_code(snapshots) == EXIT_NO_USABLE_DATA


def test_exit_code_is_two_for_degraded_only_data():
    snapshots = [snapshot("gemini", [window("daily_requests", 5.0, confidence=Confidence.ESTIMATED)])]
    assert exit_code(snapshots) == EXIT_NO_USABLE_DATA


def test_fail_on_degraded_requires_every_provider_to_be_ok():
    snapshots = [
        snapshot("claude", [window("five_hour", 21.0)]),
        snapshot("gemini", [window("daily_requests", 5.0, confidence=Confidence.ESTIMATED)]),
    ]
    assert exit_code(snapshots) == EXIT_OK
    assert exit_code(snapshots, fail_on_degraded=True) == EXIT_NO_USABLE_DATA


def test_fail_on_degraded_still_passes_when_all_are_ok():
    snapshots = [
        snapshot("claude", [window("five_hour", 21.0)]),
        snapshot("gemini", [window("daily_requests", 5.0)]),
    ]
    assert exit_code(snapshots, fail_on_degraded=True) == EXIT_OK
