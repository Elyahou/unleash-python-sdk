"""Embedded pure-Python evaluation engine.

``UnleashEngine`` is a drop-in replacement for the previously used native
engine, exposing exactly the API surface the SDK uses. It holds no native binary and never serializes the context to JSON on
the evaluation path: state is pre-compiled once in :meth:`take_state` and every
``is_enabled`` / ``get_variant`` call operates on plain dicts in-process.

The facade is intentionally thin — it owns the engine's mutable state
(``EngineState`` + ``MetricsBucket``) and delegates all logic to the sibling
modules:

* :mod:`UnleashClient.engine.state` — parse/serialize the pre-compiled model
* :mod:`UnleashClient.engine.strategies` — built-in strategy evaluation
* :mod:`UnleashClient.engine.constraints` — constraint operators
* :mod:`UnleashClient.engine.variants` — variant selection
* :mod:`UnleashClient.engine.dependencies` — dependent-feature resolution
* :mod:`UnleashClient.engine.metrics` — toggle/variant counters
* :mod:`UnleashClient.engine.hashing` — murmur3 normalized hashing
"""

from __future__ import annotations

from typing import List, Optional

from ct_unleash.engine.metrics import MetricsBucket
from ct_unleash.engine.state import (
    EngineState,
    FeatureDefinition,
    TotalParseFailure,
    parse_client_features,
    serialize_state,
)
from ct_unleash.engine.variants import (
    Variant,
    feature_enabled,
    select_variant,
)

__all__ = ["UnleashEngine", "Variant", "FeatureDefinition"]


class UnleashEngine:
    """Pure-Python feature evaluation engine (drop-in for the native engine)."""

    def __init__(self) -> None:
        """Construct an engine with empty state.

        MUST NOT raise: the SDK instantiates this eagerly before any state is
        loaded.
        """
        self._state: EngineState = EngineState()
        self._metrics: MetricsBucket = MetricsBucket()

    # -- state management ---------------------------------------------------

    def take_state(self, state_json: str) -> Optional[str]:
        """Parse and install a new ClientFeatures state.

        A *total* parse failure (invalid JSON, or valid JSON that is not an
        object) leaves the prior good state untouched — matching the native
        engine, which never wipes its loaded features on a bad fetch. Only the
        warnings are surfaced; the existing state keeps evaluating. Per-feature
        degradation still applies normally (the offending feature is skipped).

        :param state_json: Raw ClientFeatures JSON (flat or delta-event form).
        :return: A newline-joined warnings string for unparseable entries, or
            ``None`` when there are no warnings (matching the native engine).
        """
        prior_raw = self._state.raw if self._state is not None else None
        try:
            new_state, warnings = parse_client_features(
                state_json, prior_raw, raise_on_total_failure=True
            )
        except TotalParseFailure as exc:
            # Preserve the prior good state; do not wipe loaded features.
            return "\n".join(exc.warnings) if exc.warnings else None
        self._state = new_state
        if warnings:
            return "\n".join(warnings)
        return None

    def get_state(self) -> str:
        """Serialize the current state to a ClientFeatures JSON string.

        Used by the streaming connector cache; round-trips through
        :meth:`take_state` without changing evaluation behavior.
        """
        return serialize_state(self._state)

    # -- evaluation ---------------------------------------------------------

    def is_enabled(self, toggle_name: str, context: dict) -> Optional[bool]:
        """Evaluate whether a toggle is enabled for the given context.

        :param toggle_name: The feature name.
        :param context: The evaluation context (a plain dict).
        :return: ``True``/``False`` for known toggles, or ``None`` when the
            toggle is unknown (triggers the SDK's fallback path).
        """
        feature = self._state.get_feature(toggle_name)
        if feature is None:
            return None
        return feature_enabled(feature, context, self._state)

    def get_variant(self, toggle_name: str, context: dict) -> Optional[Variant]:
        """Resolve the variant for a toggle and context.

        :param toggle_name: The feature name.
        :param context: The evaluation context (a plain dict).
        :return: A :class:`Variant` (``asdict``-compatible with the native one), or
            ``None`` when the toggle is unknown.
        """
        feature = self._state.get_feature(toggle_name)
        if feature is None:
            return None
        return select_variant(feature, context, self._state)

    # -- metrics ------------------------------------------------------------

    def count_toggle(self, toggle_name: str, enabled: bool) -> None:
        """Record a toggle evaluation in the metrics bucket."""
        self._metrics.count_toggle(toggle_name, enabled)

    def count_variant(self, toggle_name: str, variant_name: str) -> None:
        """Record a variant selection in the metrics bucket."""
        self._metrics.count_variant(toggle_name, variant_name)

    def get_metrics(self) -> Optional[dict]:
        """Snapshot-and-reset the metrics bucket.

        :return: The metrics bucket dict, or a falsy value when there has been
            no activity since the last snapshot (so ``send_metrics`` skips
            submission).
        """
        bucket = self._metrics.snapshot()
        if bucket and bucket.get("toggles"):
            return bucket
        return None

    # -- introspection ------------------------------------------------------

    def list_known_toggles(self) -> List[FeatureDefinition]:
        """Return descriptors (``name``/``type``/``project``) for known toggles."""
        return self._state.list_known_toggles()

    def should_emit_impression_event(self, toggle_name: str) -> bool:
        """Return the feature's ``impressionData`` flag (``False`` if unknown)."""
        feature = self._state.get_feature(toggle_name)
        return bool(feature.impression_data) if feature is not None else False

    # -- unsupported --------------------------------------------------------

    def register_custom_strategies(self, custom_strategies: dict) -> None:
        """Custom strategies are not supported by the embedded engine."""
        raise ValueError("Custom strategies are not supported by the embedded engine")
