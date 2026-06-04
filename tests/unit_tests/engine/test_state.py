"""Unit tests for ``UnleashClient.engine.state``.

Covers:

* parsing a ``ClientFeatures`` payload into the pre-compiled model;
* segment pre-resolution into strategy constraints (spec ``15-global-constraints``);
* per-constraint value pre-parsing (numeric / semver / date / case-folded lists);
* variant cumulative-weight precomputation;
* dependency parsing;
* resilient warnings (an unparseable entry warns but never aborts the payload);
* ``list_known_toggles`` shape;
* lossless-enough serialization round-trip (``take_state(get_state())`` parity).

State snippets are embedded directly (derived from the client-spec JSONs) so the
tests pin behavior independently of the full client.
"""

from __future__ import annotations

import json
from datetime import datetime

from UnleashClient.engine.state import (
    FeatureDefinition,
    parse_client_features,
    serialize_state,
)

# ---------------------------------------------------------------------------
# Basic parsing
# ---------------------------------------------------------------------------


def test_parse_empty_state():
    state, warnings = parse_client_features(json.dumps({"version": 2, "features": []}))
    assert state.features == {}
    assert warnings == []


def test_parse_simple_feature():
    payload = {
        "version": 2,
        "features": [
            {
                "name": "Feature.A",
                "type": "release",
                "enabled": True,
                "project": "myProject",
                "impressionData": True,
                "strategies": [{"name": "default"}],
            }
        ],
    }
    state, warnings = parse_client_features(json.dumps(payload))
    assert warnings == []
    feature = state.get_feature("Feature.A")
    assert feature is not None
    assert feature.enabled is True
    assert feature.type == "release"
    assert feature.project == "myProject"
    assert feature.impression_data is True
    assert len(feature.strategies) == 1
    assert feature.strategies[0].name == "default"


def test_project_defaults_when_absent():
    payload = {
        "version": 2,
        "features": [{"name": "F", "enabled": True}],
    }
    state, _ = parse_client_features(json.dumps(payload))
    assert state.get_feature("F").project == "default"


def test_unknown_feature_returns_none():
    state, _ = parse_client_features(json.dumps({"version": 2, "features": []}))
    assert state.get_feature("nope") is None


# ---------------------------------------------------------------------------
# Constraint pre-parsing
# ---------------------------------------------------------------------------


def _single_constraint(constraint_dict):
    payload = {
        "version": 2,
        "features": [
            {
                "name": "F",
                "enabled": True,
                "strategies": [{"name": "default", "constraints": [constraint_dict]}],
            }
        ],
    }
    state, warnings = parse_client_features(json.dumps(payload))
    feature = state.get_feature("F")
    return feature.strategies[0].constraints[0], warnings


def test_constraint_basic_fields():
    c, warnings = _single_constraint(
        {"contextName": "userId", "operator": "IN", "values": ["a", "b"]}
    )
    assert warnings == []
    assert c.context_name == "userId"
    assert c.operator == "IN"
    assert c.values == ["a", "b"]
    assert c.case_insensitive is False
    assert c.inverted is False


def test_constraint_inverted_and_case_insensitive():
    c, _ = _single_constraint(
        {
            "contextName": "x",
            "operator": "STR_CONTAINS",
            "values": ["AbC"],
            "caseInsensitive": True,
            "inverted": True,
        }
    )
    assert c.case_insensitive is True
    assert c.inverted is True
    # Pre-folded values must match the fold the constraints module applies to
    # the context value (``str.upper()``), so they actually compare equal.
    assert c.parsed_values == ["ABC"]


def test_case_sensitive_string_constraint_has_no_prefold():
    c, _ = _single_constraint(
        {"contextName": "x", "operator": "STR_CONTAINS", "values": ["AbC"]}
    )
    assert c.parsed_values is None


def test_numeric_constraint_preparsed():
    c, _ = _single_constraint({"contextName": "n", "operator": "NUM_LTE", "value": "4"})
    assert c.value == "4"
    assert c.parsed_number == 4.0


def test_semver_constraint_preparsed():
    c, _ = _single_constraint(
        {"contextName": "version", "operator": "SEMVER_EQ", "value": "1.2.2"}
    )
    assert c.value == "1.2.2"
    assert c.parsed_semver is not None
    # str() of a parsed semver round-trips to the original.
    assert str(c.parsed_semver) == "1.2.2"


def test_date_constraint_preparsed():
    c, _ = _single_constraint(
        {
            "contextName": "currentTime",
            "operator": "DATE_AFTER",
            "value": "2022-01-22T15:00:00.000Z",
        }
    )
    assert isinstance(c.parsed_date, datetime)
    assert c.parsed_date.tzinfo is not None


def test_unparseable_semver_value_does_not_crash():
    # Bad constraint value must not abort the payload; constraint is still
    # retained (constraints.py decides the eval result), parsed_semver stays None.
    c, warnings = _single_constraint(
        {"contextName": "version", "operator": "SEMVER_EQ", "value": "not-a-version"}
    )
    assert c.parsed_semver is None
    # A warning is acceptable but the feature must survive.


# ---------------------------------------------------------------------------
# Segment resolution
# ---------------------------------------------------------------------------


def test_segments_resolved_into_strategy_constraints():
    payload = {
        "version": 2,
        "features": [
            {
                "name": "F",
                "enabled": True,
                "strategies": [
                    {
                        "name": "default",
                        "constraints": [
                            {"contextName": "a", "operator": "IN", "values": ["1"]}
                        ],
                        "segments": [1],
                    }
                ],
            }
        ],
        "segments": [
            {
                "id": 1,
                "constraints": [
                    {"contextName": "b", "operator": "IN", "values": ["2"]}
                ],
            }
        ],
    }
    state, warnings = parse_client_features(json.dumps(payload))
    assert warnings == []
    strategy = state.get_feature("F").strategies[0]
    assert strategy.segment_ids == [1]
    context_names = {c.context_name for c in strategy.constraints}
    # Strategy's own constraint + the segment's constraint are both present.
    assert context_names == {"a", "b"}
    assert strategy.has_missing_segment is False


def test_missing_segment_flagged():
    payload = {
        "version": 2,
        "features": [
            {
                "name": "F",
                "enabled": True,
                "strategies": [{"name": "default", "segments": [99]}],
            }
        ],
        "segments": [],
    }
    state, _ = parse_client_features(json.dumps(payload))
    strategy = state.get_feature("F").strategies[0]
    assert strategy.has_missing_segment is True


# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------


def test_feature_variants_cumulative_weights():
    payload = {
        "version": 2,
        "features": [
            {
                "name": "F",
                "enabled": True,
                "strategies": [{"name": "default"}],
                "variants": [
                    {"name": "a", "weight": 25},
                    {"name": "b", "weight": 25},
                    {"name": "c", "weight": 50},
                ],
            }
        ],
    }
    state, _ = parse_client_features(json.dumps(payload))
    variants = state.get_feature("F").variants
    assert [v.cumulative_weight for v in variants] == [25, 50, 100]


def test_variant_overrides_and_stickiness_and_payload():
    payload = {
        "version": 2,
        "features": [
            {
                "name": "F",
                "enabled": True,
                "strategies": [{"name": "default"}],
                "variants": [
                    {
                        "name": "blue",
                        "weight": 100,
                        "stickiness": "customField",
                        "payload": {"type": "string", "value": "v"},
                        "overrides": [{"contextName": "userId", "values": ["1", "2"]}],
                    }
                ],
            }
        ],
    }
    state, _ = parse_client_features(json.dumps(payload))
    variant = state.get_feature("F").variants[0]
    assert variant.stickiness == "customField"
    assert variant.payload == {"type": "string", "value": "v"}
    assert len(variant.overrides) == 1
    assert variant.overrides[0].context_name == "userId"
    assert variant.overrides[0].values == ["1", "2"]


def test_strategy_variants_parsed():
    payload = {
        "version": 2,
        "features": [
            {
                "name": "F",
                "enabled": True,
                "strategies": [
                    {
                        "name": "flexibleRollout",
                        "parameters": {"rollout": "100", "groupId": "g"},
                        "variants": [{"name": "sv", "weight": 100}],
                    }
                ],
            }
        ],
    }
    state, _ = parse_client_features(json.dumps(payload))
    strategy = state.get_feature("F").strategies[0]
    assert len(strategy.variants) == 1
    assert strategy.variants[0].name == "sv"
    assert strategy.variants[0].cumulative_weight == 100


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def test_dependencies_parsed():
    payload = {
        "version": 2,
        "features": [
            {
                "name": "child",
                "enabled": True,
                "strategies": [{"name": "default"}],
                "dependencies": [
                    {"feature": "p1"},
                    {"feature": "p2", "enabled": False},
                    {"feature": "p3", "variants": ["x", "y"]},
                ],
            }
        ],
    }
    state, _ = parse_client_features(json.dumps(payload))
    deps = state.get_feature("child").dependencies
    assert len(deps) == 3
    assert deps[0].feature == "p1"
    assert deps[0].enabled is True  # default
    assert deps[0].variants == []
    assert deps[1].enabled is False
    assert deps[2].variants == ["x", "y"]


# ---------------------------------------------------------------------------
# Warnings / resilience
# ---------------------------------------------------------------------------


def test_feature_missing_name_warns_and_is_skipped():
    payload = {
        "version": 2,
        "features": [
            {"enabled": True},  # no name -> unparseable
            {"name": "good", "enabled": True, "strategies": [{"name": "default"}]},
        ],
    }
    state, warnings = parse_client_features(json.dumps(payload))
    assert state.get_feature("good") is not None
    assert len(warnings) >= 1
    assert all(isinstance(w, str) for w in warnings)


def test_malformed_top_level_json_does_not_raise():
    # take_state contract: per-entry resilience; here the whole payload is junk.
    state, warnings = parse_client_features("{ not valid json ")
    # State is empty, a warning is produced, but no exception escapes.
    assert state.features == {}
    assert len(warnings) >= 1


# ---------------------------------------------------------------------------
# list_known_toggles
# ---------------------------------------------------------------------------


def test_list_known_toggles_shape():
    payload = {
        "version": 2,
        "features": [
            {"name": "A", "type": "release", "enabled": True, "project": "p1"},
            {"name": "B", "enabled": False},
        ],
    }
    state, _ = parse_client_features(json.dumps(payload))
    toggles = state.list_known_toggles()
    assert all(isinstance(t, FeatureDefinition) for t in toggles)
    names = {t.name for t in toggles}
    assert names == {"A", "B"}
    by_name = {t.name: t for t in toggles}
    assert by_name["A"].type == "release"
    assert by_name["A"].project == "p1"
    assert by_name["B"].project == "default"


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------


def _round_trip_equivalent(payload):
    state, warnings = parse_client_features(json.dumps(payload))
    assert warnings == []
    serialized = serialize_state(state)
    assert isinstance(serialized, str)
    state2, warnings2 = parse_client_features(serialized)
    assert warnings2 == []
    return state, state2


def test_round_trip_simple():
    payload = {
        "version": 2,
        "features": [
            {
                "name": "F",
                "type": "release",
                "enabled": True,
                "project": "p",
                "impressionData": True,
                "strategies": [
                    {
                        "name": "flexibleRollout",
                        "parameters": {"rollout": "50", "groupId": "g"},
                        "constraints": [
                            {
                                "contextName": "version",
                                "operator": "SEMVER_GT",
                                "value": "1.0.0",
                            }
                        ],
                    }
                ],
                "variants": [
                    {"name": "a", "weight": 50},
                    {"name": "b", "weight": 50},
                ],
            }
        ],
    }
    s1, s2 = _round_trip_equivalent(payload)
    f1 = s1.get_feature("F")
    f2 = s2.get_feature("F")
    assert f1.enabled == f2.enabled
    assert f1.project == f2.project
    assert f1.impression_data == f2.impression_data
    assert f1.strategies[0].name == f2.strategies[0].name
    assert f1.strategies[0].parameters == f2.strategies[0].parameters
    assert (
        f1.strategies[0].constraints[0].operator
        == f2.strategies[0].constraints[0].operator
    )
    assert [v.cumulative_weight for v in f1.variants] == [
        v.cumulative_weight for v in f2.variants
    ]


def test_round_trip_with_segments_preserves_constraint_resolution():
    payload = {
        "version": 2,
        "features": [
            {
                "name": "F",
                "enabled": True,
                "strategies": [
                    {"name": "default", "segments": [1]},
                ],
            }
        ],
        "segments": [
            {
                "id": 1,
                "constraints": [
                    {"contextName": "b", "operator": "IN", "values": ["2"]}
                ],
            }
        ],
    }
    s1, s2 = _round_trip_equivalent(payload)
    s1_constraints = {
        c.context_name for c in s1.get_feature("F").strategies[0].constraints
    }
    s2_constraints = {
        c.context_name for c in s2.get_feature("F").strategies[0].constraints
    }
    assert s1_constraints == s2_constraints == {"b"}


def test_round_trip_preserves_missing_segment_flag():
    # A strategy referencing a non-existent segment must keep
    # ``has_missing_segment`` across serialization (spec 15: it forces False).
    payload = {
        "version": 2,
        "features": [
            {
                "name": "F",
                "enabled": True,
                "strategies": [{"name": "default", "segments": [99]}],
            }
        ],
        "segments": [],
    }
    state, _ = parse_client_features(json.dumps(payload))
    serialized = serialize_state(state)
    state2, warnings2 = parse_client_features(serialized)
    assert warnings2 == []
    assert state2.get_feature("F").strategies[0].has_missing_segment is True


def test_round_trip_with_dependencies():
    payload = {
        "version": 2,
        "features": [
            {"name": "p", "enabled": True, "strategies": [{"name": "default"}]},
            {
                "name": "c",
                "enabled": True,
                "strategies": [{"name": "default"}],
                "dependencies": [{"feature": "p", "variants": ["x"]}],
            },
        ],
    }
    s1, s2 = _round_trip_equivalent(payload)
    d1 = s1.get_feature("c").dependencies[0]
    d2 = s2.get_feature("c").dependencies[0]
    assert d1.feature == d2.feature
    assert d1.variants == d2.variants
