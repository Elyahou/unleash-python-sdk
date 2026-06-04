"""Variant selection for the embedded engine.

Selects the variant for a feature given a context, handling:

* feature-level variants vs strategy-level variants (strategy variants from the
  matched enabling strategy take precedence over feature variants);
* weighted distribution using the precomputed cumulative weight table and the
  variant hash seed;
* stickiness (``default`` -> userId then sessionId then random fallback;
  ``random``; or a custom context field);
* variant overrides (first matching override wins, before weighting);
* payload passthrough and the ``feature_enabled`` / ``enabled`` flags on the
  returned variant.

The returned :class:`Variant` is the drop-in replacement for the native engine's
``Variant`` dataclass: the SDK consumes it via ``dataclasses.asdict`` and reads
the keys ``name``, ``payload``, ``enabled`` and ``feature_enabled``.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from UnleashClient.engine.hashing import VARIANT_HASH_SEED, normalized_hash
from UnleashClient.engine.state import (
    EngineState,
    FeatureState,
    StrategyDef,
    VariantDef,
)


@dataclass
class Variant:
    """Result of variant selection — shape-compatible with the native ``Variant``.

    The SDK serializes this via ``asdict`` and drops ``None`` values, so field
    names must match exactly:

    * ``name`` — variant name (``"disabled"`` for the disabled variation).
    * ``payload`` — optional ``{"type", "value"}`` payload.
    * ``enabled`` — whether this variant (not the feature) is enabled.
    * ``feature_enabled`` — whether the underlying feature evaluated enabled.
    """

    name: str
    payload: Optional[Dict[str, str]] = field(default_factory=dict)
    enabled: bool = False
    feature_enabled: bool = False


#: The variant returned when a feature is unknown, disabled, or has no variants.
DISABLED_VARIANT: Variant = Variant(
    name="disabled", payload=None, enabled=False, feature_enabled=False
)


def _context_value(context: dict, context_name: str) -> Optional[str]:
    """Resolve a context field: top-level first, then ``properties``.

    Mirrors the identifier lookup used everywhere else in the engine. Returns
    ``None`` when absent. Values are returned as-is (callers stringify).
    """
    if context_name in context:
        return context[context_name]
    properties = context.get("properties")
    if isinstance(properties, dict) and context_name in properties:
        return properties[context_name]
    return None


def _resolve_stickiness_seed(context: dict, stickiness: Optional[str]) -> str:
    """Compute the stickiness identifier used to hash a variant selection.

    * ``default`` (or unset): ``userId`` -> ``sessionId`` -> random fallback.
      (The native engine does NOT consider ``remoteAddress`` for variant
      stickiness — only userId then sessionId, then random.)
    * ``random``: a fresh random value every call.
    * any other value: a custom context field (top-level or ``properties``);
      falls back to random when the field is absent.
    """
    selector = stickiness or "default"

    if selector == "default":
        for key in ("userId", "sessionId"):
            value = context.get(key)
            if value is not None:
                return str(value)
        return str(random.random() * 10000)

    if selector == "random":
        return str(random.random() * 10000)

    value = _context_value(context, selector)
    if value is not None:
        return str(value)
    return str(random.random() * 10000)


def _apply_overrides(variants: List[VariantDef], context: dict) -> Optional[VariantDef]:
    """Return the variant whose override matches the context, if any.

    Matches the native engine (yggdrasil): the *first* matching override wins.
    Variants are scanned in declaration order and the first variant with any
    override matching the context is returned immediately.
    """
    for variant in variants:
        for override in variant.overrides:
            value = _context_value(context, override.context_name)
            if value is not None and str(value) in override.values:
                return variant
    return None


def _build_variant(variant: VariantDef, feature_enabled: bool) -> Variant:
    """Materialize a :class:`Variant` from a compiled :class:`VariantDef`."""
    return Variant(
        name=variant.name,
        payload=variant.payload,
        enabled=True,
        feature_enabled=feature_enabled,
    )


def select_from_variants(
    variants: List[VariantDef],
    group: str,
    context: dict,
    feature_enabled: bool,
    stickiness: Optional[str] = None,
) -> Variant:
    """Pure weighted variant selection (no strategy/feature gating).

    Selection order:

    1. Variant overrides (a matching override forces that variant).
    2. Weighted distribution: ``normalized_hash`` over the total weight using the
       :data:`VARIANT_HASH_SEED`, picking the first variant whose running
       cumulative weight reaches the hashed target.

    Stickiness resolution: an explicit ``stickiness`` argument takes precedence
    (used for strategy-level variants, which inherit the *strategy's* stickiness
    per spec ``16-strategy-variants``); otherwise the first variant's own
    ``stickiness`` field is used (feature-level / custom-stickiness variants).

    Returns the disabled variant (carrying ``feature_enabled``) when there are no
    variants or the total weight is non-positive.
    """
    if not variants:
        return Variant(
            name="disabled",
            payload=None,
            enabled=False,
            feature_enabled=feature_enabled,
        )

    override = _apply_overrides(variants, context)
    if override is not None:
        return _build_variant(override, feature_enabled)

    total_weight = variants[-1].cumulative_weight
    if total_weight <= 0:
        # Defensive: fall back to summing if cumulative weights were not
        # precomputed for some reason.
        total_weight = sum(v.weight for v in variants)
    if total_weight <= 0:
        return Variant(
            name="disabled",
            payload=None,
            enabled=False,
            feature_enabled=feature_enabled,
        )

    if stickiness is None:
        stickiness = variants[0].stickiness
    identifier = _resolve_stickiness_seed(context, stickiness)
    target = normalized_hash(group, identifier, total_weight, seed=VARIANT_HASH_SEED)

    counter = 0
    for variant in variants:
        counter += variant.weight
        if counter >= target:
            return _build_variant(variant, feature_enabled)

    # Unreachable for positive weights, but keep a safe fallback.
    return _build_variant(variants[-1], feature_enabled)


def _evaluate_feature(
    feature: FeatureState,
    context: dict,
    state: EngineState,
) -> tuple:
    """Resolve feature-enabled state and the variant-driving strategy.

    Returns ``(feature_enabled, matched_strategy)`` where ``matched_strategy`` is
    the strategy whose variants drive variant selection: the **first matched
    strategy that itself declares variants**, falling back to the first matched
    strategy when none of the matched strategies have variants (in which case
    feature-level variants apply). Returns ``(True, None)`` for a feature with no
    strategies and ``(False, None)`` when disabled / no strategy matched.

    Matching the native engine: with OR-of-strategies, a matched strategy that
    carries variants is preferred over an earlier matched strategy that does not,
    so strategy variants win over feature variants whenever *any* enabling
    strategy defines them. Strategy and dependency evaluation are delegated to
    the sibling modules so the variant module stays free of evaluation logic.
    """
    # Imported lazily to break the import cycle: ``dependencies`` and
    # ``strategies`` both import from this module.
    from UnleashClient.engine.dependencies import (  # noqa: PLC0415
        dependencies_satisfied,
    )
    from UnleashClient.engine.strategies import evaluate_strategy  # noqa: PLC0415

    if not feature.enabled:
        return False, None

    if not dependencies_satisfied(feature, context, state):
        return False, None

    if not feature.strategies:
        # No strategies => enabled (not expressible via the UI, but defined).
        return True, None

    first_matched: Optional[StrategyDef] = None
    for strategy in feature.strategies:
        if evaluate_strategy(strategy, context, state):
            if strategy.variants:
                # First matched strategy that carries variants wins outright.
                return True, strategy
            if first_matched is None:
                first_matched = strategy

    if first_matched is not None:
        return True, first_matched

    return False, None


def feature_enabled(
    feature: FeatureState,
    context: dict,
    state: EngineState,
) -> bool:
    """Return whether ``feature`` evaluates enabled for ``context``.

    Thin wrapper over :func:`_evaluate_feature` exposing only the enabled verdict
    (strategies + dependencies), used by the engine facade's ``is_enabled``.
    """
    enabled, _ = _evaluate_feature(feature, context, state)
    return enabled


def select_variant(
    feature: FeatureState,
    context: dict,
    state: EngineState,
) -> Optional[Variant]:
    """Select the variant for a feature given a context.

    Determines feature-enabled state internally (strategies + dependencies),
    then chooses between strategy-level variants (from the matched enabling
    strategy, hashed on that strategy's ``groupId``) and feature-level variants
    (hashed on the feature name). Returns the disabled variant — carrying the
    correct ``feature_enabled`` flag — when no variant applies. Returns ``None``
    only for an unknown (``None``) feature.

    :param feature: The compiled feature.
    :param context: The evaluation context.
    :param state: The full engine state.
    :return: The selected :class:`Variant`, or ``None`` for unknown features.
    """
    if feature is None:
        return None

    feature_enabled, matched_strategy = _evaluate_feature(feature, context, state)

    if not feature_enabled:
        return DISABLED_VARIANT

    # Strategy variants (from the enabling strategy) take precedence over
    # feature variants; otherwise fall back to feature-level variants.
    if matched_strategy is not None and matched_strategy.variants:
        group = _strategy_group(matched_strategy, feature)
        # Strategy-level variants inherit the *strategy's* stickiness (spec
        # ``16-strategy-variants``), not any per-variant ``stickiness`` field.
        # When the strategy declares no stickiness parameter the resolution
        # falls back to ``"default"`` (userId -> sessionId -> ...), matching the
        # native engine — which ignores the per-variant stickiness entirely.
        strategy_stickiness = matched_strategy.parameters.get("stickiness") or "default"
        return select_from_variants(
            matched_strategy.variants,
            group,
            context,
            feature_enabled=True,
            stickiness=strategy_stickiness,
        )

    if feature.variants:
        return select_from_variants(
            feature.variants, feature.name, context, feature_enabled=True
        )

    # Feature is enabled but has no applicable variants.
    return Variant(name="disabled", payload=None, enabled=False, feature_enabled=True)


def _strategy_group(strategy: StrategyDef, feature: FeatureState) -> str:
    """Hash group for strategy variants.

    The strategy's ``groupId`` is used as-is *when present* — including an empty
    string, which the native engine hashes on literally. Only a wholly absent
    ``groupId`` falls back to the feature name.
    """
    group_id = strategy.parameters.get("groupId")
    if group_id is None:
        return feature.name
    return group_id
