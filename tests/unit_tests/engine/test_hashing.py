"""Unit tests for the embedded engine's murmur3 normalized hashing.

All expectations are derived directly from the official client-specification
suite (``08-variants``, ``12-custom-stickiness``, ``16-strategy-variants`` for
the variant seed; ``10-flexible-rollout``/``12-custom-stickiness`` for the
rollout seed). The hash function itself has no spec JSON; instead we pin the
exact integer outcomes the spec tests imply so any seam (seed, group ordering,
``+1`` offset, modulo) regression is caught here.
"""

import mmh3
import pytest

from UnleashClient.engine.hashing import VARIANT_HASH_SEED, normalized_hash


def _reference(group, identifier, normalizer, seed):
    """Independent reference of the canonical Unleash hash formula."""
    return mmh3.hash(f"{group}:{identifier}", seed, signed=False) % normalizer + 1


class TestNormalizedHashFormula:
    def test_matches_canonical_formula(self):
        assert normalized_hash("group", "id", 100, 0) == _reference(
            "group", "id", 100, 0
        )

    def test_default_normalizer_is_100(self):
        assert normalized_hash("group", "id") == _reference("group", "id", 100, 0)

    def test_default_seed_is_zero(self):
        assert normalized_hash("group", "id", 100) == normalized_hash(
            "group", "id", 100, 0
        )

    def test_result_is_in_inclusive_range(self):
        # Sweep many identifiers and confirm the [1, normalizer] invariant.
        for i in range(2000):
            value = normalized_hash("feature", str(i), 100, 0)
            assert 1 <= value <= 100

    def test_normalizer_one_always_returns_one(self):
        for i in range(50):
            assert normalized_hash("g", str(i), 1, 0) == 1

    def test_group_and_identifier_not_interchangeable(self):
        # "a:b" must differ from "b:a" in the general case (ordering matters).
        assert normalized_hash("a", "b", 1000, 0) != normalized_hash("b", "a", 1000, 0)


class TestVariantSeed:
    """The variant distribution seed is 86028157 (distinct from rollout seed)."""

    def test_variant_hash_seed_constant(self):
        assert VARIANT_HASH_SEED == 86028157

    @pytest.mark.parametrize(
        "group,identifier,total_weight,expected",
        [
            # 08-variants Feature.Variants.B (two weight-1 variants).
            ("Feature.Variants.B", "0", 2, 1),
            ("Feature.Variants.B", "2", 2, 2),
            # 08-variants Feature.Variants.C (three weight-33 variants).
            ("Feature.Variants.C", "232", 99, 18),
            ("Feature.Variants.C", "607", 99, 58),
            ("Feature.Variants.C", "656", 99, 94),
            # 08-variants Feature.Variants.override.D non-override user 388.
            ("Feature.Variants.override.D", "388", 100, 55),
            # 12-custom-stickiness 100% rollout, four weight-25 variants.
            ("Feature.flexible.rollout.custom.stickiness_100", "616", 100, 15),
            ("Feature.flexible.rollout.custom.stickiness_100", "503", 100, 35),
            ("Feature.flexible.rollout.custom.stickiness_100", "438", 100, 60),
            ("Feature.flexible.rollout.custom.stickiness_100", "44", 100, 98),
        ],
    )
    def test_variant_targets_match_spec(
        self, group, identifier, total_weight, expected
    ):
        assert (
            normalized_hash(group, identifier, total_weight, seed=VARIANT_HASH_SEED)
            == expected
        )

    def test_strategy_variant_group_is_group_id(self):
        # 16-strategy-variants uses groupId "a"; userId 5 -> first variant (<=1),
        # userId 0 -> second variant (==2) for two weight-1 variants.
        assert normalized_hash("a", "5", 2, seed=VARIANT_HASH_SEED) == 1
        assert normalized_hash("a", "0", 2, seed=VARIANT_HASH_SEED) == 2


class TestRolloutSeed:
    """Gradual / flexible rollout uses the default seed of 0."""

    @pytest.mark.parametrize(
        "group,identifier,expected_le_50",
        [
            # 12-custom-stickiness _50 rollout, groupId = feature name.
            ("Feature.flexible.rollout.custom.stickiness_50", "402", False),
            ("Feature.flexible.rollout.custom.stickiness_50", "388", True),
            ("Feature.flexible.rollout.custom.stickiness_50", "39", True),
        ],
    )
    def test_rollout_threshold_matches_spec(self, group, identifier, expected_le_50):
        # 50% rollout: enabled iff normalized_hash(...) <= 50 (seed 0).
        value = normalized_hash(group, identifier, 100, seed=0)
        assert (value <= 50) is expected_le_50

    def test_rollout_seed_differs_from_variant_seed(self):
        # The two seeds must produce a different DISTRIBUTION, otherwise the
        # rollout/variant distinction would be meaningless. A single input can
        # coincidentally land in the same [1, normalizer] bucket under both
        # seeds (different hashes, same modulo bucket), so we compare the full
        # distribution across many identifiers rather than a single point.
        rollout = [normalized_hash("group", str(i), 100, seed=0) for i in range(200)]
        variant = [
            normalized_hash("group", str(i), 100, seed=VARIANT_HASH_SEED)
            for i in range(200)
        ]
        assert rollout != variant
