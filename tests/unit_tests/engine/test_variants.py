"""Unit tests for the embedded engine's variant selection.

Cases are derived directly from the official Unleash client-specification suite
(``08-variants``, ``16-strategy-variants``, ``12-custom-stickiness``). The
relevant state snippets are embedded inline and compiled through
``parse_client_features`` so the tests exercise the real precomputed
cumulative-weight tables and the real ``VariantDef`` shape, then call
``select_variant`` / ``select_from_variants`` directly (not the full client).

The variant contract under test:

* unknown feature -> ``None``;
* disabled / not-enabled feature -> ``DISABLED_VARIANT`` (``feature_enabled`` False);
* enabled feature with empty/zero-weight variants -> a disabled variant carrying
  ``feature_enabled=True``;
* weighted distribution uses ``VARIANT_HASH_SEED`` and the cumulative table;
* overrides take precedence over weighting;
* strategy-level variants (from the matched enabling strategy) take precedence
  over feature-level variants; fall back to feature variants when absent;
* stickiness: ``default`` (userId -> sessionId -> remoteAddress -> random),
  ``random``, and custom context fields (top-level or under ``properties``);
* payload passthrough and the ``enabled`` / ``feature_enabled`` flags.
"""

from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from UnleashClient.engine.hashing import VARIANT_HASH_SEED, normalized_hash
from UnleashClient.engine.state import (
    EngineState,
    VariantDef,
    VariantOverride,
    parse_client_features,
)
from UnleashClient.engine.variants import (
    DISABLED_VARIANT,
    Variant,
    select_from_variants,
    select_variant,
)

# ===========================================================================
# Spec fixtures: 08-variants and 16-strategy-variants embedded inline
# ===========================================================================

VARIANTS_08 = {
    "version": 1,
    "features": [
        {
            "name": "Feature.Variants.A",
            "enabled": True,
            "strategies": [{"name": "default", "parameters": {}}],
            "variants": [
                {
                    "name": "variant1",
                    "weight": 1,
                    "payload": {"type": "string", "value": "val1"},
                }
            ],
        },
        {
            "name": "Feature.Variants.B",
            "enabled": True,
            "strategies": [],
            "variants": [
                {
                    "name": "variant1",
                    "weight": 1,
                    "payload": {"type": "string", "value": "val1"},
                },
                {
                    "name": "variant2",
                    "weight": 1,
                    "payload": {"type": "string", "value": "val2"},
                },
            ],
        },
        {
            "name": "Feature.Variants.C",
            "enabled": True,
            "strategies": [],
            "variants": [
                {
                    "name": "variant1",
                    "weight": 33,
                    "payload": {"type": "string", "value": "val1"},
                },
                {
                    "name": "variant2",
                    "weight": 33,
                    "payload": {"type": "string", "value": "val2"},
                },
                {
                    "name": "variant3",
                    "weight": 33,
                    "payload": {"type": "string", "value": "val3"},
                },
            ],
        },
        {
            "name": "Feature.Variants.D",
            "enabled": True,
            "strategies": [],
            "variants": [
                {
                    "name": "variant1",
                    "weight": 1,
                    "payload": {"type": "string", "value": "val1"},
                },
                {
                    "name": "variant2",
                    "weight": 49,
                    "payload": {"type": "string", "value": "val2"},
                },
                {
                    "name": "variant3",
                    "weight": 50,
                    "payload": {"type": "string", "value": "val3"},
                },
            ],
        },
        {
            "name": "Feature.Variants.override.D",
            "enabled": True,
            "strategies": [],
            "variants": [
                {
                    "name": "variant1",
                    "weight": 33,
                    "payload": {"type": "string", "value": "val1"},
                    "overrides": [{"contextName": "userId", "values": ["132", "61"]}],
                },
                {
                    "name": "variant2",
                    "weight": 33,
                    "payload": {"type": "string", "value": "val2"},
                },
                {
                    "name": "variant3",
                    "weight": 34,
                    "payload": {"type": "string", "value": "val3"},
                },
            ],
        },
        {
            "name": "Feature.Variants.E",
            "enabled": False,
            "strategies": [{"name": "default", "parameters": {}}],
            "variants": [
                {
                    "name": "variant1",
                    "weight": 1,
                    "payload": {"type": "string", "value": "val1"},
                }
            ],
        },
        {
            "name": "Feature.Variants.F",
            "enabled": True,
            "strategies": [{"name": "default", "parameters": {}}],
            "variants": [],
        },
        {
            "name": "Feature.Variants.G",
            "enabled": True,
            "strategies": [
                {
                    "name": "flexibleRollout",
                    "parameters": {
                        "rollout": "0",
                        "stickiness": "default",
                        "groupId": "feature-variant-g",
                    },
                }
            ],
            "variants": [
                {
                    "name": "variant1",
                    "weight": 1,
                    "payload": {"type": "string", "value": "val1"},
                }
            ],
        },
        {
            "name": "Feature.Variants.H",
            "enabled": True,
            "strategies": [{"name": "default", "parameters": {}}],
            "variants": [
                {
                    "name": "variant1",
                    "weight": 1,
                    "payload": {"type": "number", "value": "1.2"},
                }
            ],
        },
    ],
}

STRATEGY_VARIANTS_16 = {
    "version": 1,
    "features": [
        {
            "name": "Feature.strategy.variant.with.constraint",
            "enabled": True,
            "strategies": [
                {
                    "name": "flexibleRollout",
                    "variants": [
                        {
                            "name": "variantName",
                            "weight": 1,
                            "payload": {"type": "string", "value": "variantValue"},
                        }
                    ],
                    "parameters": {
                        "rollout": "100",
                        "stickiness": "default",
                        "groupId": "a",
                    },
                    "constraints": [
                        {
                            "contextName": "environment",
                            "operator": "IN",
                            "values": ["dev"],
                        }
                    ],
                }
            ],
            "variants": [],
        },
        {
            "name": "Feature.strategy.variant.overrides.feature.variant",
            "enabled": True,
            "strategies": [
                {
                    "name": "flexibleRollout",
                    "parameters": {
                        "rollout": "100",
                        "stickiness": "default",
                        "groupId": "a",
                    },
                    "variants": [
                        {
                            "name": "variantName",
                            "weight": 1,
                            "payload": {"type": "string", "value": "variantValue"},
                        }
                    ],
                    "constraints": [
                        {
                            "contextName": "environment",
                            "operator": "IN",
                            "values": ["dev"],
                        }
                    ],
                }
            ],
            "variants": [
                {
                    "name": "featureVariant",
                    "weight": 1,
                    "payload": {"type": "string", "value": "willBeIgnored"},
                }
            ],
        },
        {
            "name": "Feature.strategy.multiple.variants",
            "enabled": True,
            "strategies": [
                {
                    "name": "flexibleRollout",
                    "parameters": {
                        "rollout": "100",
                        "stickiness": "default",
                        "groupId": "a",
                    },
                    "variants": [
                        {
                            "name": "variantNameA",
                            "weight": 1,
                            "payload": {"type": "string", "value": "variantValueA"},
                        },
                        {
                            "name": "variantNameB",
                            "weight": 1,
                            "payload": {"type": "string", "value": "variantValueB"},
                        },
                    ],
                    "constraints": [],
                }
            ],
            "variants": [],
        },
        {
            "name": "Feature.strategy.empty.variants",
            "enabled": True,
            "strategies": [
                {
                    "name": "flexibleRollout",
                    "variants": [],
                    "parameters": {
                        "rollout": "100",
                        "stickiness": "default",
                        "groupId": "a",
                    },
                    "constraints": [],
                }
            ],
            "variants": [],
        },
        {
            "name": "Feature.strategy.variant.fallback",
            "enabled": True,
            "strategies": [
                {
                    "name": "flexibleRollout",
                    "parameters": {
                        "rollout": "100",
                        "stickiness": "default",
                        "groupId": "a",
                    },
                }
            ],
            "variants": [
                {
                    "name": "featureVariant",
                    "weight": 1,
                    "payload": {"type": "string", "value": "fallbackValue"},
                }
            ],
        },
        {
            "name": "Feature.strategy.variants.stickiness",
            "enabled": True,
            "strategies": [
                {
                    "name": "flexibleRollout",
                    "parameters": {
                        "rollout": "100",
                        "stickiness": "clientId",
                        "groupId": "a",
                    },
                    "variants": [
                        {
                            "name": "variantNameA",
                            "weight": 1,
                            "payload": {"type": "string", "value": "variantValueA"},
                        },
                        {
                            "name": "variantNameB",
                            "weight": 1,
                            "payload": {"type": "string", "value": "variantValueB"},
                        },
                    ],
                    "constraints": [],
                }
            ],
            "variants": [],
        },
    ],
}


@pytest.fixture(scope="module")
def state_08() -> EngineState:
    state, warnings = parse_client_features(json.dumps(VARIANTS_08))
    assert warnings == []
    return state


@pytest.fixture(scope="module")
def state_16() -> EngineState:
    state, warnings = parse_client_features(json.dumps(STRATEGY_VARIANTS_16))
    assert warnings == []
    return state


def _select(state: EngineState, name: str, context: dict):
    feature = state.get_feature(name)
    return select_variant(feature, context, state)


# ===========================================================================
# Variant dataclass shape (consumed via asdict, None values dropped by SDK)
# ===========================================================================


class TestVariantShape:
    def test_field_names_and_defaults(self):
        v = Variant(name="x")
        d = asdict(v)
        assert set(d) == {"name", "payload", "enabled", "feature_enabled"}
        assert d == {
            "name": "x",
            "payload": {},
            "enabled": False,
            "feature_enabled": False,
        }

    def test_disabled_variant_constant(self):
        assert DISABLED_VARIANT.name == "disabled"
        assert DISABLED_VARIANT.payload is None
        assert DISABLED_VARIANT.enabled is False
        assert DISABLED_VARIANT.feature_enabled is False


# ===========================================================================
# select_variant — feature-level (08-variants)
# ===========================================================================


class TestSelectVariantFeatureLevel:
    def test_unknown_feature_returns_none(self, state_08):
        assert select_variant(None, {}, state_08) is None

    def test_missing_toggle_via_state_lookup(self, state_08):
        # state.get_feature returns None for unknown -> select_variant returns None.
        assert _select(state_08, "Feature.Variants.MissingToggle", {}) is None

    def test_single_variant_always_selected(self, state_08):
        v = _select(state_08, "Feature.Variants.A", {"userId": "0"})
        assert v == Variant(
            name="variant1",
            payload={"type": "string", "value": "val1"},
            enabled=True,
            feature_enabled=True,
        )

    def test_two_variants_user_0(self, state_08):
        v = _select(state_08, "Feature.Variants.B", {"userId": "0"})
        assert v.name == "variant1"
        assert v.enabled is True and v.feature_enabled is True

    def test_two_variants_user_2(self, state_08):
        v = _select(state_08, "Feature.Variants.B", {"userId": "2"})
        assert v.name == "variant2"

    @pytest.mark.parametrize(
        "user_id,expected",
        [
            ("232", "variant1"),
            ("607", "variant2"),
            ("656", "variant3"),
        ],
    )
    def test_three_equal_variants_distribution(self, state_08, user_id, expected):
        v = _select(state_08, "Feature.Variants.C", {"userId": user_id})
        assert v.name == expected

    @pytest.mark.parametrize(
        "user_id,expected",
        [
            ("712", "variant1"),
            ("525", "variant2"),
            ("537", "variant3"),
        ],
    )
    def test_weighted_variants_distribution(self, state_08, user_id, expected):
        v = _select(state_08, "Feature.Variants.D", {"userId": user_id})
        assert v.name == expected

    def test_numeric_payload_passthrough(self, state_08):
        v = _select(state_08, "Feature.Variants.H", {"userId": "0"})
        assert v.payload == {"type": "number", "value": "1.2"}

    def test_disabled_feature_returns_disabled_variant(self, state_08):
        v = _select(state_08, "Feature.Variants.E", {"userId": "0"})
        assert v.name == "disabled"
        assert v.enabled is False
        assert v.feature_enabled is False

    def test_enabled_feature_empty_variants_feature_enabled_true(self, state_08):
        v = _select(state_08, "Feature.Variants.F", {"userId": "0"})
        assert v.name == "disabled"
        assert v.enabled is False
        # Feature evaluated enabled even though there are no variants.
        assert v.feature_enabled is True

    def test_zero_rollout_with_variants_is_disabled(self, state_08):
        # Strategy evaluates false (0% rollout) -> feature not enabled.
        v = _select(state_08, "Feature.Variants.G", {"userId": "0"})
        assert v.name == "disabled"
        assert v.enabled is False
        assert v.feature_enabled is False


# ===========================================================================
# Overrides take precedence (08-variants override.D)
# ===========================================================================


class TestOverrides:
    @pytest.mark.parametrize("user_id", ["132", "61"])
    def test_override_forces_variant1(self, state_08, user_id):
        v = _select(state_08, "Feature.Variants.override.D", {"userId": user_id})
        assert v.name == "variant1"

    def test_non_override_user_falls_to_weighting(self, state_08):
        # userId 388 is not in the override list -> weighted selection -> variant2.
        v = _select(state_08, "Feature.Variants.override.D", {"userId": "388"})
        assert v.name == "variant2"

    def test_override_checks_properties_too(self):
        variants = [
            VariantDef(
                name="a",
                weight=1,
                cumulative_weight=1,
                overrides=[VariantOverride(context_name="custom", values=["x"])],
            ),
            VariantDef(name="b", weight=1, cumulative_weight=2),
        ]
        # Override field looked up under properties.
        v = select_from_variants(
            variants, "g", {"properties": {"custom": "x"}}, feature_enabled=True
        )
        assert v.name == "a"

    def test_last_matching_override_wins(self):
        # Two variants both override on userId=5; the LAST one wins (legacy parity).
        variants = [
            VariantDef(
                name="a",
                weight=1,
                cumulative_weight=1,
                overrides=[VariantOverride(context_name="userId", values=["5"])],
            ),
            VariantDef(
                name="b",
                weight=1,
                cumulative_weight=2,
                overrides=[VariantOverride(context_name="userId", values=["5"])],
            ),
        ]
        v = select_from_variants(variants, "g", {"userId": "5"}, feature_enabled=True)
        assert v.name == "b"


# ===========================================================================
# select_variant — strategy-level variants (16-strategy-variants)
# ===========================================================================


class TestStrategyVariants:
    def test_strategy_variant_returned_when_strategy_matches(self, state_16):
        v = _select(
            state_16, "Feature.strategy.variant.with.constraint", {"environment": "dev"}
        )
        assert v == Variant(
            name="variantName",
            payload={"type": "string", "value": "variantValue"},
            enabled=True,
            feature_enabled=True,
        )

    def test_strategy_variant_disabled_when_strategy_fails(self, state_16):
        v = _select(
            state_16,
            "Feature.strategy.variant.with.constraint",
            {"environment": "not-dev"},
        )
        assert v.name == "disabled"
        assert v.feature_enabled is False

    def test_strategy_variant_overrides_feature_variant(self, state_16):
        # Feature has a featureVariant, but the matched strategy's variant wins.
        v = _select(
            state_16,
            "Feature.strategy.variant.overrides.feature.variant",
            {"environment": "dev"},
        )
        assert v.name == "variantName"
        assert v.payload == {"type": "string", "value": "variantValue"}

    @pytest.mark.parametrize(
        "user_id,expected",
        [
            ("5", "variantNameA"),
            ("0", "variantNameB"),
        ],
    )
    def test_multiple_strategy_variants_distribution(self, state_16, user_id, expected):
        v = _select(state_16, "Feature.strategy.multiple.variants", {"userId": user_id})
        assert v.name == expected

    def test_empty_strategy_variants_disabled_feature_enabled(self, state_16):
        v = _select(state_16, "Feature.strategy.empty.variants", {})
        assert v.name == "disabled"
        assert v.enabled is False
        assert v.feature_enabled is True

    def test_falls_back_to_feature_variants(self, state_16):
        # Strategy has no variants -> feature variants are used.
        v = _select(state_16, "Feature.strategy.variant.fallback", {})
        assert v.name == "featureVariant"
        assert v.payload == {"type": "string", "value": "fallbackValue"}

    @pytest.mark.parametrize(
        "client_id,expected",
        [
            ("1", "variantNameA"),
            ("2", "variantNameB"),
        ],
    )
    def test_strategy_variant_inherits_strategy_stickiness(
        self, state_16, client_id, expected
    ):
        v = _select(
            state_16,
            "Feature.strategy.variants.stickiness",
            {"properties": {"clientId": client_id}},
        )
        assert v.name == expected


# ===========================================================================
# select_from_variants — pure weighted selection unit tests
# ===========================================================================


class TestSelectFromVariants:
    def test_empty_variants_disabled(self):
        v = select_from_variants([], "g", {}, feature_enabled=True)
        assert v.name == "disabled"
        assert v.feature_enabled is True
        assert v.enabled is False

    def test_zero_total_weight_disabled(self):
        variants = [VariantDef(name="a", weight=0, cumulative_weight=0)]
        v = select_from_variants(variants, "g", {"userId": "1"}, feature_enabled=True)
        assert v.name == "disabled"
        assert v.feature_enabled is True

    def test_cumulative_table_scan_matches_hash(self):
        # Three weight-33 variants (total 99); selection must equal the bucket
        # the hash lands in. Cross-check against normalized_hash directly.
        variants = [
            VariantDef(name="v1", weight=33, cumulative_weight=33),
            VariantDef(name="v2", weight=33, cumulative_weight=66),
            VariantDef(name="v3", weight=33, cumulative_weight=99),
        ]
        for user_id in ["232", "607", "656", "1", "99", "1000"]:
            target = normalized_hash(
                "Feature.Variants.C", user_id, 99, seed=VARIANT_HASH_SEED
            )
            if target <= 33:
                expected = "v1"
            elif target <= 66:
                expected = "v2"
            else:
                expected = "v3"
            v = select_from_variants(
                variants,
                "Feature.Variants.C",
                {"userId": user_id},
                feature_enabled=True,
            )
            assert v.name == expected, (user_id, target)

    def test_feature_enabled_flag_propagated(self):
        variants = [VariantDef(name="v1", weight=1, cumulative_weight=1)]
        v = select_from_variants(variants, "g", {"userId": "1"}, feature_enabled=True)
        assert v.feature_enabled is True
        assert v.enabled is True

    def test_uses_variant_hash_seed_not_zero(self):
        # If select_from_variants used seed=0 it would land in a different bucket
        # for some identifier; pin one where the two seeds disagree.
        variants = [
            VariantDef(name="v1", weight=1, cumulative_weight=1),
            VariantDef(name="v2", weight=1, cumulative_weight=2),
        ]
        # Find an id where rollout-seed and variant-seed buckets differ.
        chosen = None
        for i in range(100):
            sid = str(i)
            t0 = normalized_hash("a", sid, 2, seed=0)
            tv = normalized_hash("a", sid, 2, seed=VARIANT_HASH_SEED)
            if t0 != tv:
                chosen = (sid, "v1" if tv == 1 else "v2")
                break
        assert chosen is not None
        sid, expected = chosen
        v = select_from_variants(variants, "a", {"userId": sid}, feature_enabled=True)
        assert v.name == expected


# ===========================================================================
# Stickiness resolution (default chain, random, custom)
# ===========================================================================


class TestStickiness:
    def test_custom_stickiness_distribution(self):
        # 12-custom-stickiness: four weight-25 variants, stickiness=customField.
        variants = [
            VariantDef(
                name="blue", weight=25, cumulative_weight=25, stickiness="customField"
            ),
            VariantDef(
                name="red", weight=25, cumulative_weight=50, stickiness="customField"
            ),
            VariantDef(
                name="green", weight=25, cumulative_weight=75, stickiness="customField"
            ),
            VariantDef(
                name="yellow",
                weight=25,
                cumulative_weight=100,
                stickiness="customField",
            ),
        ]
        group = "Feature.flexible.rollout.custom.stickiness_100"
        cases = {"616": "blue", "503": "red", "438": "green", "44": "yellow"}
        for field_value, expected in cases.items():
            v = select_from_variants(
                variants,
                group,
                {"properties": {"customField": field_value}},
                feature_enabled=True,
            )
            assert v.name == expected, (field_value, expected)

    def test_default_stickiness_uses_user_id(self):
        # Two variants, default stickiness -> identifier comes from userId.
        variants = [
            VariantDef(name="a", weight=1, cumulative_weight=1),
            VariantDef(name="b", weight=1, cumulative_weight=2),
        ]
        v_user = select_from_variants(
            variants, "g", {"userId": "42"}, feature_enabled=True
        )
        # Same identifier supplied via sessionId only should match the userId
        # result when userId is absent (both fall through to the same seed).
        v_session = select_from_variants(
            variants, "g", {"sessionId": "42"}, feature_enabled=True
        )
        assert v_user.name == v_session.name

    def test_default_stickiness_prefers_user_over_session(self):
        variants = [
            VariantDef(name="a", weight=1, cumulative_weight=1),
            VariantDef(name="b", weight=1, cumulative_weight=2),
        ]
        # userId present -> sessionId ignored. Result must equal userId-only run.
        both = select_from_variants(
            variants, "g", {"userId": "42", "sessionId": "999"}, feature_enabled=True
        )
        user_only = select_from_variants(
            variants, "g", {"userId": "42"}, feature_enabled=True
        )
        assert both.name == user_only.name

    def test_random_stickiness_still_returns_a_variant(self):
        variants = [
            VariantDef(name="a", weight=1, cumulative_weight=1, stickiness="random"),
            VariantDef(name="b", weight=1, cumulative_weight=2, stickiness="random"),
        ]
        v = select_from_variants(variants, "g", {}, feature_enabled=True)
        assert v.name in {"a", "b"}
        assert v.enabled is True

    def test_default_stickiness_no_identifier_random_fallback(self):
        # No userId/sessionId/remoteAddress -> random fallback, still a variant.
        variants = [
            VariantDef(name="a", weight=1, cumulative_weight=1),
            VariantDef(name="b", weight=1, cumulative_weight=2),
        ]
        v = select_from_variants(variants, "g", {}, feature_enabled=True)
        assert v.name in {"a", "b"}

    def test_missing_custom_field_falls_back_to_random(self):
        variants = [
            VariantDef(
                name="a", weight=1, cumulative_weight=1, stickiness="customField"
            ),
            VariantDef(
                name="b", weight=1, cumulative_weight=2, stickiness="customField"
            ),
        ]
        # customField absent -> random fallback, still returns a real variant.
        v = select_from_variants(variants, "g", {}, feature_enabled=True)
        assert v.name in {"a", "b"}


# ===========================================================================
# Strategy group selection for strategy variants
# ===========================================================================


class TestStrategyGroup:
    def test_strategy_group_uses_group_id(self, state_16):
        # 16-strategy-variants all use groupId "a"; multiple.variants user 5 -> A,
        # user 0 -> B. This pins that the hash group is the groupId, not the
        # feature name (which would land in a different bucket).
        feature = state_16.get_feature("Feature.strategy.multiple.variants")
        strategy = feature.strategies[0]
        assert strategy.parameters.get("groupId") == "a"
        v5 = _select(state_16, "Feature.strategy.multiple.variants", {"userId": "5"})
        v0 = _select(state_16, "Feature.strategy.multiple.variants", {"userId": "0"})
        # Confirm against the groupId-based hash directly.
        t5 = normalized_hash("a", "5", 2, seed=VARIANT_HASH_SEED)
        t0 = normalized_hash("a", "0", 2, seed=VARIANT_HASH_SEED)
        assert v5.name == ("variantNameA" if t5 == 1 else "variantNameB")
        assert v0.name == ("variantNameA" if t0 == 1 else "variantNameB")
