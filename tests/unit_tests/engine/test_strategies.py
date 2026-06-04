"""Unit tests for ``UnleashClient.engine.strategies``.

These test the built-in strategy evaluators directly (not the full client).
Cases are derived from the official client-specification JSONs under
``tests/specification_tests/client-specification/specifications`` (the relevant
state snippets are embedded inline below).

Because the sibling modules ``constraints`` and ``hashing`` are implemented in
parallel, the tests monkeypatch them via the ``strategies`` module namespace:

* ``strategies.evaluate_constraint`` is patched to a controllable stub for the
  constraint/segment-gate tests.
* ``strategies.normalized_hash`` is patched to a local reference implementation
  (the frozen murmur3 formula) so rollout tests are deterministic regardless of
  the hashing agent's progress.
"""

from __future__ import annotations

import mmh3
import pytest

from ct_unleash.engine import strategies
from ct_unleash.engine.state import (
    ConstraintDef,
    EngineState,
    StrategyDef,
)

# ---------------------------------------------------------------------------
# Reference helpers
# ---------------------------------------------------------------------------


def _ref_normalized_hash(group, identifier, normalizer=100, seed=0):
    """Reference murmur3 normalized hash (frozen formula)."""
    return mmh3.hash(f"{group}:{identifier}", seed, signed=False) % normalizer + 1


@pytest.fixture
def patch_hash(monkeypatch):
    """Install the reference normalized_hash into the strategies module."""
    monkeypatch.setattr(strategies, "normalized_hash", _ref_normalized_hash)
    return _ref_normalized_hash


@pytest.fixture
def empty_state():
    return EngineState()


def _strategy(
    name, parameters=None, constraints=None, segment_ids=None, has_missing_segment=False
):
    return StrategyDef(
        name=name,
        parameters=parameters or {},
        constraints=constraints or [],
        segment_ids=segment_ids or [],
        has_missing_segment=has_missing_segment,
    )


# ---------------------------------------------------------------------------
# default strategy
# ---------------------------------------------------------------------------


def test_default_always_true(empty_state):
    assert strategies.apply_default(_strategy("default"), {}, empty_state) is True


def test_evaluate_default_dispatches_true(empty_state):
    assert strategies.evaluate_strategy(_strategy("default"), {}, empty_state) is True


# ---------------------------------------------------------------------------
# userWithId strategy  (spec 02-user-with-id-strategy)
# ---------------------------------------------------------------------------


def test_user_with_id_member(empty_state):
    s = _strategy("userWithId", {"userIds": "123"})
    assert strategies.apply_user_with_id(s, {"userId": "123"}, empty_state) is True


def test_user_with_id_non_member(empty_state):
    s = _strategy("userWithId", {"userIds": "123"})
    assert strategies.apply_user_with_id(s, {"userId": "22"}, empty_state) is False


def test_user_with_id_missing_user(empty_state):
    s = _strategy("userWithId", {"userIds": "123"})
    assert strategies.apply_user_with_id(s, {}, empty_state) is False


def test_user_with_id_csv_with_spaces(empty_state):
    # "123, 222, 88" must be trimmed per element (spec Feature.D2).
    s = _strategy("userWithId", {"userIds": "123, 222, 88"})
    assert strategies.apply_user_with_id(s, {"userId": "222"}, empty_state) is True
    assert strategies.apply_user_with_id(s, {"userId": "88"}, empty_state) is True


def test_user_with_id_empty_param(empty_state):
    s = _strategy("userWithId", {"userIds": ""})
    assert strategies.apply_user_with_id(s, {"userId": "123"}, empty_state) is False


def test_user_with_id_missing_param(empty_state):
    # No userIds parameter at all -> treated as no members.
    s = _strategy("userWithId", {})
    assert strategies.apply_user_with_id(s, {"userId": "123"}, empty_state) is False


# ---------------------------------------------------------------------------
# gradualRolloutUserId  (spec 03)
# ---------------------------------------------------------------------------


def test_gradual_rollout_user_id_100(patch_hash, empty_state):
    s = _strategy("gradualRolloutUserId", {"percentage": "100", "groupId": "AB12A"})
    assert (
        strategies.apply_gradual_rollout_user_id(s, {"userId": "123"}, empty_state)
        is True
    )


def test_gradual_rollout_user_id_0(patch_hash, empty_state):
    s = _strategy("gradualRolloutUserId", {"percentage": "0", "groupId": "AB12A"})
    assert (
        strategies.apply_gradual_rollout_user_id(s, {"userId": "122"}, empty_state)
        is False
    )


def test_gradual_rollout_user_id_50_in(patch_hash, empty_state):
    # userId=122 with groupId AB12A hashes to 23 (<=50) -> enabled.
    s = _strategy("gradualRolloutUserId", {"percentage": "50", "groupId": "AB12A"})
    assert (
        strategies.apply_gradual_rollout_user_id(s, {"userId": "122"}, empty_state)
        is True
    )


def test_gradual_rollout_user_id_50_out(patch_hash, empty_state):
    # userId=155 with groupId AB12A hashes to 100 (>50) -> disabled.
    s = _strategy("gradualRolloutUserId", {"percentage": "50", "groupId": "AB12A"})
    assert (
        strategies.apply_gradual_rollout_user_id(s, {"userId": "155"}, empty_state)
        is False
    )


def test_gradual_rollout_user_id_missing_user(patch_hash, empty_state):
    s = _strategy("gradualRolloutUserId", {"percentage": "100", "groupId": "AB12A"})
    assert strategies.apply_gradual_rollout_user_id(s, {}, empty_state) is False


# ---------------------------------------------------------------------------
# gradualRolloutSessionId  (spec 04)
# ---------------------------------------------------------------------------


def test_gradual_rollout_session_id_100(patch_hash, empty_state):
    s = _strategy("gradualRolloutSessionId", {"percentage": "100", "groupId": "AB12A"})
    assert (
        strategies.apply_gradual_rollout_session_id(
            s, {"sessionId": "123"}, empty_state
        )
        is True
    )


def test_gradual_rollout_session_id_50_in(patch_hash, empty_state):
    s = _strategy("gradualRolloutSessionId", {"percentage": "50", "groupId": "AB12A"})
    assert (
        strategies.apply_gradual_rollout_session_id(
            s, {"sessionId": "122"}, empty_state
        )
        is True
    )


def test_gradual_rollout_session_id_50_out(patch_hash, empty_state):
    s = _strategy("gradualRolloutSessionId", {"percentage": "50", "groupId": "AB12A"})
    assert (
        strategies.apply_gradual_rollout_session_id(
            s, {"sessionId": "155"}, empty_state
        )
        is False
    )


def test_gradual_rollout_session_id_missing_session(patch_hash, empty_state):
    s = _strategy("gradualRolloutSessionId", {"percentage": "100", "groupId": "AB12A"})
    assert strategies.apply_gradual_rollout_session_id(s, {}, empty_state) is False


# ---------------------------------------------------------------------------
# gradualRolloutRandom  (spec 05)
# ---------------------------------------------------------------------------


def test_gradual_rollout_random_100(empty_state):
    s = _strategy("gradualRolloutRandom", {"percentage": "100"})
    # 100% -> always enabled regardless of randomness.
    assert all(
        strategies.apply_gradual_rollout_random(s, {}, empty_state) is True
        for _ in range(50)
    )


def test_gradual_rollout_random_0(empty_state):
    s = _strategy("gradualRolloutRandom", {"percentage": "0"})
    # 0% -> always disabled.
    assert all(
        strategies.apply_gradual_rollout_random(s, {}, empty_state) is False
        for _ in range(50)
    )


def test_gradual_rollout_random_deterministic_with_patched_rng(
    monkeypatch, empty_state
):
    # Force the random draw to a fixed value to check the boundary comparison.
    monkeypatch.setattr(strategies.random, "randint", lambda a, b: 30)
    s50 = _strategy("gradualRolloutRandom", {"percentage": "50"})
    s20 = _strategy("gradualRolloutRandom", {"percentage": "20"})
    assert strategies.apply_gradual_rollout_random(s50, {}, empty_state) is True
    assert strategies.apply_gradual_rollout_random(s20, {}, empty_state) is False


# ---------------------------------------------------------------------------
# flexibleRollout  (spec 10 + 12 custom stickiness)
# ---------------------------------------------------------------------------


def test_flexible_rollout_100(patch_hash, empty_state):
    s = _strategy(
        "flexibleRollout",
        {
            "rollout": "100",
            "stickiness": "default",
            "groupId": "Feature.flexibleRollout.100",
        },
    )
    assert strategies.apply_flexible_rollout(s, {}, empty_state) is True


def test_flexible_rollout_0(patch_hash, empty_state):
    s = _strategy(
        "flexibleRollout",
        {
            "rollout": "0",
            "stickiness": "default",
            "groupId": "Feature.flexibleRollout.0",
        },
    )
    assert (
        strategies.apply_flexible_rollout(
            s, {"sessionId": "147", "userId": "12"}, empty_state
        )
        is False
    )


def test_flexible_rollout_default_userid_in(patch_hash, empty_state):
    # userId=174 / groupId Feature.flexibleRollout.10 -> hash 10 (<=10) enabled.
    s = _strategy(
        "flexibleRollout",
        {
            "rollout": "10",
            "stickiness": "default",
            "groupId": "Feature.flexibleRollout.10",
        },
    )
    assert strategies.apply_flexible_rollout(s, {"userId": "174"}, empty_state) is True


def test_flexible_rollout_default_userid_out(patch_hash, empty_state):
    # userId=499 -> hash 32 (>10) disabled.
    s = _strategy(
        "flexibleRollout",
        {
            "rollout": "10",
            "stickiness": "default",
            "groupId": "Feature.flexibleRollout.10",
        },
    )
    assert strategies.apply_flexible_rollout(s, {"userId": "499"}, empty_state) is False


def test_flexible_rollout_default_falls_back_to_session(patch_hash, empty_state):
    # No userId, sessionId=174 -> uses sessionId, hash 10 (<=10) enabled.
    s = _strategy(
        "flexibleRollout",
        {
            "rollout": "10",
            "stickiness": "default",
            "groupId": "Feature.flexibleRollout.10",
        },
    )
    assert (
        strategies.apply_flexible_rollout(s, {"sessionId": "174"}, empty_state) is True
    )
    s2 = _strategy(
        "flexibleRollout",
        {
            "rollout": "10",
            "stickiness": "default",
            "groupId": "Feature.flexibleRollout.10",
        },
    )
    assert (
        strategies.apply_flexible_rollout(s2, {"sessionId": "499"}, empty_state)
        is False
    )


def test_flexible_rollout_stickiness_userid_requires_userid(patch_hash, empty_state):
    # stickiness=userId, rollout 55, groupId Feature.flexibleRollout.userId.55.
    s = _strategy(
        "flexibleRollout",
        {
            "rollout": "55",
            "stickiness": "userId",
            "groupId": "Feature.flexibleRollout.userId.55",
        },
    )
    # userId=25 -> hash 55 (<=55) enabled.
    assert strategies.apply_flexible_rollout(s, {"userId": "25"}, empty_state) is True
    # Only sessionId present -> userId stickiness has no identifier -> disabled.
    assert (
        strategies.apply_flexible_rollout(s, {"sessionId": "25"}, empty_state) is False
    )
    # No userId at all -> disabled.
    assert strategies.apply_flexible_rollout(s, {}, empty_state) is False


def test_flexible_rollout_stickiness_sessionid(patch_hash, empty_state):
    s = _strategy(
        "flexibleRollout",
        {
            "rollout": "42",
            "stickiness": "sessionId",
            "groupId": "Feature.flexibleRollout.sessionId.42",
        },
    )
    # sessionId=147 expected enabled per spec.
    assert (
        strategies.apply_flexible_rollout(s, {"sessionId": "147"}, empty_state) is True
    )


def test_flexible_rollout_default_no_id_random(monkeypatch, patch_hash, empty_state):
    # Default stickiness, no userId/sessionId -> random hash fallback.
    monkeypatch.setattr(strategies.random, "randint", lambda a, b: 5)
    s = _strategy(
        "flexibleRollout", {"rollout": "10", "stickiness": "default", "groupId": "grp"}
    )
    assert strategies.apply_flexible_rollout(s, {}, empty_state) is True
    monkeypatch.setattr(strategies.random, "randint", lambda a, b: 50)
    assert strategies.apply_flexible_rollout(s, {}, empty_state) is False


def test_flexible_rollout_groupid_defaults_to_feature_name(patch_hash, empty_state):
    # When groupId param is absent, it should default to the feature name passed
    # through parameters under the conventional key... but the strategy only has
    # parameters. The spec always supplies groupId; here we assert that a missing
    # groupId falls back to empty string deterministically (no crash) and is
    # still computable.
    s = _strategy("flexibleRollout", {"rollout": "100", "stickiness": "default"})
    # rollout 100 -> always enabled regardless of group.
    assert strategies.apply_flexible_rollout(s, {"userId": "1"}, empty_state) is True


def test_flexible_rollout_custom_stickiness_present(patch_hash, empty_state):
    # spec 12: stickiness_50, customField in properties.
    grp = "Feature.flexible.rollout.custom.stickiness_50"
    s = _strategy(
        "flexibleRollout",
        {"rollout": "50", "stickiness": "customField", "groupId": grp},
    )
    # customField=388 -> hash 10 (<=50) enabled.
    assert (
        strategies.apply_flexible_rollout(
            s, {"properties": {"customField": "388"}}, empty_state
        )
        is True
    )
    # customField=402 -> hash 55 (>50) disabled.
    assert (
        strategies.apply_flexible_rollout(
            s, {"properties": {"customField": "402"}}, empty_state
        )
        is False
    )
    # customField=39 -> hash 25 (<=50) enabled.
    assert (
        strategies.apply_flexible_rollout(
            s, {"properties": {"customField": "39"}}, empty_state
        )
        is True
    )


def test_flexible_rollout_custom_stickiness_top_level(patch_hash, empty_state):
    # Custom field can also be top-level (get_context_value checks top-level first).
    grp = "Feature.flexible.rollout.custom.stickiness_50"
    s = _strategy(
        "flexibleRollout",
        {"rollout": "50", "stickiness": "customField", "groupId": grp},
    )
    assert (
        strategies.apply_flexible_rollout(s, {"customField": "388"}, empty_state)
        is True
    )


def test_flexible_rollout_custom_stickiness_missing_field_deterministic(
    patch_hash, empty_state
):
    # spec 12: stickiness_50 with empty context -> deterministically disabled
    # (empty identifier hashes to 57 > 50). Must NOT use random.
    grp = "Feature.flexible.rollout.custom.stickiness_50"
    s = _strategy(
        "flexibleRollout",
        {"rollout": "50", "stickiness": "customField", "groupId": grp},
    )
    results = {strategies.apply_flexible_rollout(s, {}, empty_state) for _ in range(20)}
    assert results == {False}


def test_flexible_rollout_custom_stickiness_100_field_present(patch_hash, empty_state):
    # spec 12: stickiness_100 enabled when the custom field IS supplied.
    # NB: the spec test's description says "without field defined" but its
    # context actually provides customField — do not trust the prose.
    grp = "Feature.flexible.rollout.custom.stickiness_100"
    s = _strategy(
        "flexibleRollout",
        {"rollout": "100", "stickiness": "customField", "groupId": grp},
    )
    assert (
        strategies.apply_flexible_rollout(
            s, {"properties": {"customField": "any_value"}}, empty_state
        )
        is True
    )


def test_flexible_rollout_custom_stickiness_100_missing_field(patch_hash, empty_state):
    # Yggdrasil parity (caught by production differential testing): a custom
    # stickiness field missing from the context disables the strategy even at
    # rollout=100 — the 100% short-circuit must not bypass identifier
    # resolution.
    s = _strategy(
        "flexibleRollout",
        {"rollout": "100", "stickiness": "company_id", "groupId": "g"},
    )
    assert strategies.apply_flexible_rollout(s, {}, empty_state) is False
    assert (
        strategies.apply_flexible_rollout(s, {"properties": {}}, empty_state) is False
    )
    assert (
        strategies.apply_flexible_rollout(
            s, {"userId": "u1", "properties": {}}, empty_state
        )
        is False
    )


def test_flexible_rollout_explicit_userid_stickiness_100_missing_user(
    patch_hash, empty_state
):
    # Yggdrasil parity: explicit "userId" stickiness is a named field, not
    # "default" — a missing userId disables the strategy even at rollout=100.
    s = _strategy(
        "flexibleRollout",
        {"rollout": "100", "stickiness": "userId", "groupId": "g"},
    )
    assert strategies.apply_flexible_rollout(s, {}, empty_state) is False


def test_flexible_rollout_default_and_random_stickiness_100_empty_context(
    patch_hash, empty_state
):
    # Yggdrasil parity guard: "default" and "random" stickiness DO fall back
    # to a random hash, which always passes at rollout=100.
    for stickiness in ("default", "random"):
        s = _strategy(
            "flexibleRollout",
            {"rollout": "100", "stickiness": stickiness, "groupId": "g"},
        )
        assert strategies.apply_flexible_rollout(s, {}, empty_state) is True


# ---------------------------------------------------------------------------
# remoteAddress  (spec 06)
# ---------------------------------------------------------------------------


def test_remote_address_exact_ipv4(empty_state):
    s = _strategy("remoteAddress", {"IPs": "127.0.0.1"})
    assert (
        strategies.apply_remote_address(s, {"remoteAddress": "127.0.0.1"}, empty_state)
        is True
    )


def test_remote_address_missing_context(empty_state):
    s = _strategy("remoteAddress", {"IPs": "127.0.0.1"})
    assert strategies.apply_remote_address(s, {}, empty_state) is False


def test_remote_address_list_member(empty_state):
    s = _strategy("remoteAddress", {"IPs": "192.168.0.1, 192.168.0.2, 192.168.0.3"})
    assert (
        strategies.apply_remote_address(
            s, {"remoteAddress": "192.168.0.1"}, empty_state
        )
        is True
    )
    assert (
        strategies.apply_remote_address(
            s, {"remoteAddress": "192.168.0.3"}, empty_state
        )
        is True
    )


def test_remote_address_list_non_member(empty_state):
    s = _strategy("remoteAddress", {"IPs": "192.168.0.1, 192.168.0.2, 192.168.0.3"})
    assert (
        strategies.apply_remote_address(
            s, {"remoteAddress": "217.100.10.11"}, empty_state
        )
        is False
    )


def test_remote_address_ignores_invalid_config_ips(empty_state):
    # spec Feature.remoteAddress.C: invalid entry ignored, valid ones still match.
    s = _strategy(
        "remoteAddress", {"IPs": "192.168.0.1, 192.invalid, 192.168.0.2, 192.168.0.3"}
    )
    assert (
        strategies.apply_remote_address(
            s, {"remoteAddress": "192.168.0.3"}, empty_state
        )
        is True
    )


def test_remote_address_cidr_range(empty_state):
    s = _strategy("remoteAddress", {"IPs": "160.33.0.0/16"})
    assert (
        strategies.apply_remote_address(
            s, {"remoteAddress": "160.33.0.33"}, empty_state
        )
        is True
    )
    assert (
        strategies.apply_remote_address(s, {"remoteAddress": "160.34.0.1"}, empty_state)
        is False
    )


def test_remote_address_invalid_context_ip_non_match(empty_state):
    s = _strategy("remoteAddress", {"IPs": "127.0.0.1"})
    assert (
        strategies.apply_remote_address(s, {"remoteAddress": "not-an-ip"}, empty_state)
        is False
    )


def test_remote_address_ipv6_exact(empty_state):
    s = _strategy("remoteAddress", {"IPs": "2001:db8::1"})
    assert (
        strategies.apply_remote_address(
            s, {"remoteAddress": "2001:db8::1"}, empty_state
        )
        is True
    )
    assert (
        strategies.apply_remote_address(
            s, {"remoteAddress": "2001:db8::2"}, empty_state
        )
        is False
    )


def test_remote_address_ipv6_cidr(empty_state):
    s = _strategy("remoteAddress", {"IPs": "2001:db8::/32"})
    assert (
        strategies.apply_remote_address(
            s, {"remoteAddress": "2001:db8:0:1::42"}, empty_state
        )
        is True
    )
    assert (
        strategies.apply_remote_address(
            s, {"remoteAddress": "2001:db9::1"}, empty_state
        )
        is False
    )


def test_remote_address_version_mismatch_non_match(empty_state):
    # IPv4 config, IPv6 context -> non-match (no crash).
    s = _strategy("remoteAddress", {"IPs": "127.0.0.1"})
    assert (
        strategies.apply_remote_address(
            s, {"remoteAddress": "2001:db8::1"}, empty_state
        )
        is False
    )


def test_remote_address_missing_ips_param(empty_state):
    s = _strategy("remoteAddress", {})
    assert (
        strategies.apply_remote_address(s, {"remoteAddress": "127.0.0.1"}, empty_state)
        is False
    )


# ---------------------------------------------------------------------------
# applicationHostname
# ---------------------------------------------------------------------------


def test_application_hostname_match(monkeypatch, empty_state):
    monkeypatch.setattr(strategies.platform, "node", lambda: "myhost")
    s = _strategy("applicationHostname", {"hostNames": "otherhost, myhost"})
    assert strategies.apply_application_hostname(s, {}, empty_state) is True


def test_application_hostname_no_match(monkeypatch, empty_state):
    monkeypatch.setattr(strategies.platform, "node", lambda: "myhost")
    s = _strategy("applicationHostname", {"hostNames": "otherhost"})
    assert strategies.apply_application_hostname(s, {}, empty_state) is False


def test_application_hostname_missing_param(monkeypatch, empty_state):
    monkeypatch.setattr(strategies.platform, "node", lambda: "myhost")
    s = _strategy("applicationHostname", {})
    assert strategies.apply_application_hostname(s, {}, empty_state) is False


# ---------------------------------------------------------------------------
# evaluate_strategy: constraint + segment gating
# ---------------------------------------------------------------------------


def test_evaluate_strategy_unknown_name_is_false(empty_state):
    s = _strategy("noSuchStrategy")
    assert strategies.evaluate_strategy(s, {}, empty_state) is False


def test_evaluate_strategy_missing_segment_forces_false(monkeypatch, empty_state):
    # spec 15: a strategy referencing a nonexistent segment evaluates to false,
    # even though 'default' would otherwise be true and constraints all pass.
    monkeypatch.setattr(strategies, "evaluate_constraint", lambda c, ctx: True)
    s = _strategy("default", has_missing_segment=True)
    assert strategies.evaluate_strategy(s, {}, empty_state) is False


def test_evaluate_strategy_constraints_all_pass(monkeypatch, empty_state):
    monkeypatch.setattr(strategies, "evaluate_constraint", lambda c, ctx: True)
    s = _strategy(
        "default", constraints=[ConstraintDef("version", "SEMVER_EQ", value="1.2.2")]
    )
    assert strategies.evaluate_strategy(s, {}, empty_state) is True


def test_evaluate_strategy_constraint_fails_gates_false(monkeypatch, empty_state):
    monkeypatch.setattr(strategies, "evaluate_constraint", lambda c, ctx: False)
    s = _strategy(
        "default", constraints=[ConstraintDef("version", "SEMVER_EQ", value="9.9.9")]
    )
    assert strategies.evaluate_strategy(s, {}, empty_state) is False


def test_evaluate_strategy_one_failing_constraint_gates_false(monkeypatch, empty_state):
    # Two constraints; the second fails -> whole strategy false (AND semantics).
    calls = []

    def fake(c, ctx):
        calls.append(c)
        return c.value == "ok"

    monkeypatch.setattr(strategies, "evaluate_constraint", fake)
    s = _strategy(
        "default",
        constraints=[
            ConstraintDef("a", "IN", value="ok"),
            ConstraintDef("b", "IN", value="bad"),
        ],
    )
    assert strategies.evaluate_strategy(s, {}, empty_state) is False


def test_evaluate_strategy_constraints_pass_then_strategy_logic(
    monkeypatch, patch_hash, empty_state
):
    # Constraints pass; underlying userWithId logic then decides.
    monkeypatch.setattr(strategies, "evaluate_constraint", lambda c, ctx: True)
    s = _strategy(
        "userWithId",
        {"userIds": "5"},
        constraints=[ConstraintDef("x", "IN", value="y")],
    )
    assert strategies.evaluate_strategy(s, {"userId": "5"}, empty_state) is True
    assert strategies.evaluate_strategy(s, {"userId": "6"}, empty_state) is False


def test_evaluate_strategy_no_constraints_runs_logic(empty_state):
    s = _strategy("default")
    assert strategies.evaluate_strategy(s, {}, empty_state) is True


def test_dispatch_table_populated():
    expected = {
        "default",
        "userWithId",
        "gradualRolloutUserId",
        "gradualRolloutSessionId",
        "gradualRolloutRandom",
        "flexibleRollout",
        "remoteAddress",
        "applicationHostname",
    }
    assert expected <= set(strategies.STRATEGY_DISPATCH.keys())
