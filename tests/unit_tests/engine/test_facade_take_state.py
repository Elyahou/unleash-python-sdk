"""Facade ``take_state`` resilience: a bad payload must not wipe loaded state.

Regression coverage for the silent-failure finding: a single corrupt poll /
stream response previously replaced the entire prior good state with empty
state, flipping every flag to ``None`` (SDK fallback). The native engine leaves
its internal state untouched on a top-level parse failure; the embedded engine
must do the same — surface the warnings, keep evaluating.
"""

from __future__ import annotations

import json

import pytest

from ct_unleash.engine import UnleashEngine

GOOD_STATE = json.dumps(
    {
        "version": 2,
        "features": [
            {
                "name": "f",
                "enabled": True,
                "strategies": [{"name": "default"}],
            }
        ],
    }
)


@pytest.fixture
def loaded_engine() -> UnleashEngine:
    engine = UnleashEngine()
    engine.take_state(GOOD_STATE)
    assert engine.is_enabled("f", {}) is True
    return engine


@pytest.mark.parametrize(
    "bad_payload",
    [
        "this is not json",
        "null",
        "[]",
        '"oops"',
        "42",
        '{"error": "Bad Gateway", "statusCode": 502}',
    ],
)
def test_total_parse_failure_preserves_prior_state(loaded_engine, bad_payload):
    warnings = loaded_engine.take_state(bad_payload)
    # The flag still evaluates enabled — state was NOT wiped.
    assert loaded_engine.is_enabled("f", {}) is True
    assert [t.name for t in loaded_engine.list_known_toggles()] == ["f"]
    # Warnings are surfaced so the failure is not fully silent.
    assert warnings is not None


def test_explicit_empty_features_clears_state(loaded_engine):
    # A *valid* empty ClientFeatures document legitimately clears the state.
    warnings = loaded_engine.take_state(json.dumps({"version": 2, "features": []}))
    assert warnings is None
    assert loaded_engine.is_enabled("f", {}) is None
    assert loaded_engine.list_known_toggles() == []


def test_good_payload_after_bad_payload_loads_normally(loaded_engine):
    loaded_engine.take_state("null")
    assert loaded_engine.is_enabled("f", {}) is True
    new_state = json.dumps(
        {
            "version": 2,
            "features": [
                {"name": "g", "enabled": True, "strategies": [{"name": "default"}]}
            ],
        }
    )
    assert loaded_engine.take_state(new_state) is None
    assert loaded_engine.is_enabled("g", {}) is True
    assert loaded_engine.is_enabled("f", {}) is None  # replaced by the new state


def test_get_state_after_bad_payload_returns_preserved_state(loaded_engine):
    # The streaming connector caches ``get_state()`` after ``take_state`` — it
    # must serialize the preserved good state, not an empty wiped one.
    loaded_engine.take_state("not json at all")
    serialized = loaded_engine.get_state()
    roundtrip = UnleashEngine()
    roundtrip.take_state(serialized)
    assert roundtrip.is_enabled("f", {}) is True
