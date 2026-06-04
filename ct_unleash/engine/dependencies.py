"""Dependent-feature resolution for the embedded engine.

Implements spec ``17-dependent-features``: a feature may declare dependencies on
parent features. A dependency is satisfied when:

* the parent feature exists, and
* the parent does **not** itself declare any dependencies (Unleash forbids
  transitive and cyclic dependencies — a parent that has its own dependencies
  can never satisfy a child, regardless of its current enabled state), and
* the parent's evaluated enabled state matches the dependency's expected
  ``enabled`` flag (default ``True``), and
* if the dependency lists required ``variants``, the parent's selected variant
  name is among them (only meaningful when the parent is expected enabled).

A missing parent makes the dependency (and therefore the dependent feature)
evaluate to ``False``.

The parent's enabled state and selected variant are obtained from
``variants.select_variant``: its ``feature_enabled`` flag is the parent's
enabled verdict and its ``name`` is the parent's variant. Because we short-circuit
parents that carry their own dependencies *before* evaluating them, this never
recurses back into dependency resolution.
"""

from __future__ import annotations

from ct_unleash.engine.state import EngineState, FeatureState
from ct_unleash.engine.variants import feature_enabled, select_variant


def dependencies_satisfied(  # noqa: PLR0911
    feature: FeatureState,
    context: dict,
    state: EngineState,
) -> bool:
    """Return whether all of ``feature``'s parent dependencies are satisfied.

    Features with no declared dependencies trivially return ``True``.

    :param feature: The dependent (child) feature.
    :param context: The evaluation context.
    :param state: The full engine state (for resolving parent features).
    :return: ``True`` if every dependency is satisfied, else ``False``.
    """
    if not feature.dependencies:
        return True

    for dependency in feature.dependencies:
        parent = state.get_feature(dependency.feature)

        # Missing parent => unsatisfiable.
        if parent is None:
            return False

        # Transitive / cyclic guard: a parent that itself has dependencies can
        # never satisfy a child (this also breaks dependency cycles).
        if parent.dependencies:
            return False

        # Whether this dependency needs the parent's *variant* (not just the
        # enabled verdict). Variant matching only applies when the dependency
        # lists required variants, expects the parent enabled, and the parent
        # actually defines variants somewhere (feature-level OR on any strategy).
        needs_variant = (
            bool(dependency.variants)
            and dependency.enabled
            and _parent_has_variants(parent)
        )

        # When no variant is required, resolve only the enabled verdict — the
        # cheaper path — instead of running the full variant pipeline (stickiness
        # seed, murmur3 hash, weighted scan, Variant allocation) just to discard
        # the variant name. The parent has no dependencies of its own (checked
        # above), so neither call recurses back into dependency resolution.
        if not needs_variant:
            if feature_enabled(parent, context, state) != dependency.enabled:
                return False
            continue

        parent_variant = select_variant(parent, context, state)
        parent_enabled = (
            bool(parent_variant.feature_enabled) if parent_variant else False
        )
        if parent_enabled != dependency.enabled:
            return False
        if parent_variant is None or parent_variant.name not in dependency.variants:
            return False

    return True


def _parent_has_variants(parent: FeatureState) -> bool:
    """Whether ``parent`` defines any variants (feature-level or on a strategy).

    Strategy-level variants count: a parent whose only variants live inside its
    enabling strategy still participates in dependency variant matching (spec
    ``17-dependent-features``).
    """
    if parent.variants:
        return True
    return any(strategy.variants for strategy in parent.strategies)
