"""Unit tests for ``UnleashClient.engine.dependencies``.

Cases are derived directly from the client-specification suite
``17-dependent-features.json`` (the oracle for dependent-feature semantics).
We test ``dependencies_satisfied`` against a state built from that spec, plus a
few synthetic edge cases (cycles, transitive dependencies, variant matching).

These tests exercise ``dependencies_satisfied`` directly. Because the function
must resolve the *enabled* state and selected *variant* of parent features, it
necessarily relies on the sibling ``strategies`` and ``variants`` modules; this
file therefore also serves as an integration check of the dependency seam.
"""

from __future__ import annotations

import json
import os

import pytest

from ct_unleash.engine.dependencies import dependencies_satisfied
from ct_unleash.engine.state import (
    DependencyDef,
    EngineState,
    FeatureState,
    StrategyDef,
    parse_client_features,
)

_SPEC_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "specification_tests",
    "client-specification",
    "specifications",
    "17-dependent-features.json",
)


def _load_spec_state() -> EngineState:
    with open(_SPEC_PATH, encoding="utf-8") as handle:
        spec = json.load(handle)
    state, _warnings = parse_client_features(json.dumps(spec["state"]))
    return state


@pytest.fixture(scope="module")
def spec_state() -> EngineState:
    return _load_spec_state()


# ---------------------------------------------------------------------------
# No-dependency fast path
# ---------------------------------------------------------------------------


def test_feature_without_dependencies_is_trivially_satisfied():
    feature = FeatureState(
        name="standalone",
        enabled=True,
        strategies=[StrategyDef(name="default")],
        dependencies=[],
    )
    state = EngineState(features={"standalone": feature})
    assert dependencies_satisfied(feature, {}, state) is True


# ---------------------------------------------------------------------------
# Spec-derived enabled/disabled expectations (context {})
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "toggle_name, expected",
    [
        ("parent.enabled.child.enabled", True),
        ("parent.explicitly.enabled.child.enabled", True),
        # parent.enabled.child.disabled: dependency on parent.enabled IS
        # satisfied; the child being disabled is not a dependency concern.
        ("parent.enabled.child.disabled", True),
        ("parent.disabled.child.enabled", False),
        ("parent.disabled.child.disabled", False),
        ("parent.disabled.satisfied.child.enabled", True),
        ("parent.disabled.not.satisfied.child.enabled", False),
        ("parent.single.variant.child.enabled", True),
        ("parent.multiple.variants.child.enabled", True),
        ("parent.empty.variants.child.enabled", True),
        ("parent.non.matching.variant.child.enabled", False),
        ("multiple.parents.satisfied.child.enabled", True),
        ("multiple.parents.not.satisfied.child.enabled", False),
        ("parents.not.exist.child.enabled", False),
        ("child.with.cycle", False),
        ("child.with.transitive.dependency", False),
        ("parent.default.variant.child.enabled", True),
        ("parent.disabled.child.expects.disabled.variant", False),
    ],
)
def test_spec_dependency_resolution(spec_state, toggle_name, expected):
    feature = spec_state.get_feature(toggle_name)
    assert feature is not None
    assert dependencies_satisfied(feature, {}, spec_state) is expected


def test_spec_dependency_with_context():
    state = _load_spec_state()
    child = state.get_feature("child.with.matching.constraint")
    assert dependencies_satisfied(child, {"environment": "prod"}, state) is True

    child2 = state.get_feature("child.with.non.matching.constraint")
    assert dependencies_satisfied(child2, {"environment": "dev"}, state) is False


# ---------------------------------------------------------------------------
# Targeted unit cases (synthetic states, no spec file)
# ---------------------------------------------------------------------------


def test_missing_parent_makes_child_disabled():
    child = FeatureState(
        name="child",
        enabled=True,
        dependencies=[DependencyDef(feature="ghost")],
    )
    state = EngineState(features={"child": child})
    assert dependencies_satisfied(child, {}, state) is False


def test_parent_enabled_default_expectation_true():
    parent = FeatureState(
        name="p", enabled=True, strategies=[StrategyDef(name="default")]
    )
    child = FeatureState(
        name="c", enabled=True, dependencies=[DependencyDef(feature="p")]
    )
    state = EngineState(features={"p": parent, "c": child})
    assert dependencies_satisfied(child, {}, state) is True


def test_parent_disabled_flag_makes_default_dependency_fail():
    parent = FeatureState(
        name="p", enabled=False, strategies=[StrategyDef(name="default")]
    )
    child = FeatureState(
        name="c", enabled=True, dependencies=[DependencyDef(feature="p")]
    )
    state = EngineState(features={"p": parent, "c": child})
    assert dependencies_satisfied(child, {}, state) is False


def test_explicit_enabled_false_matches_disabled_parent():
    parent = FeatureState(
        name="p", enabled=False, strategies=[StrategyDef(name="default")]
    )
    child = FeatureState(
        name="c",
        enabled=True,
        dependencies=[DependencyDef(feature="p", enabled=False)],
    )
    state = EngineState(features={"p": parent, "c": child})
    assert dependencies_satisfied(child, {}, state) is True


def test_parent_with_own_dependencies_is_never_satisfiable():
    # Transitive / cycle guard: a parent that itself has dependencies cannot
    # be depended upon, even if it is currently enabled.
    grandparent = FeatureState(
        name="gp", enabled=True, strategies=[StrategyDef(name="default")]
    )
    parent = FeatureState(
        name="p",
        enabled=True,
        strategies=[StrategyDef(name="default")],
        dependencies=[DependencyDef(feature="gp")],
    )
    child = FeatureState(
        name="c", enabled=True, dependencies=[DependencyDef(feature="p")]
    )
    state = EngineState(features={"gp": grandparent, "p": parent, "c": child})
    # p itself is enabled (gp satisfied) ...
    assert dependencies_satisfied(parent, {}, state) is True
    # ... but c depending on p is not satisfiable because p has dependencies.
    assert dependencies_satisfied(child, {}, state) is False


def test_multiple_dependencies_all_must_hold():
    enabled_parent = FeatureState(
        name="pe", enabled=True, strategies=[StrategyDef(name="default")]
    )
    disabled_parent = FeatureState(
        name="pd", enabled=False, strategies=[StrategyDef(name="default")]
    )
    child = FeatureState(
        name="c",
        enabled=True,
        dependencies=[
            DependencyDef(feature="pe"),
            DependencyDef(feature="pd", enabled=False),
        ],
    )
    state = EngineState(
        features={"pe": enabled_parent, "pd": disabled_parent, "c": child}
    )
    assert dependencies_satisfied(child, {}, state) is True

    # Flip one expectation to break it.
    child.dependencies[1] = DependencyDef(feature="pd")  # expects enabled=True
    assert dependencies_satisfied(child, {}, state) is False
