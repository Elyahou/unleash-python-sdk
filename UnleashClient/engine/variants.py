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

The returned :class:`Variant` is the drop-in replacement for the yggdrasil
``Variant`` dataclass: the SDK consumes it via ``dataclasses.asdict`` and reads
the keys ``name``, ``payload``, ``enabled`` and ``feature_enabled``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

from UnleashClient.engine.state import EngineState, FeatureState


@dataclass
class Variant:
    """Result of variant selection — shape-compatible with yggdrasil's ``Variant``.

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


def select_variant(
    feature: FeatureState,
    context: dict,
    state: EngineState,
) -> Optional[Variant]:
    """Select the variant for an (already-evaluated) feature.

    Implementations decide feature-enabled state internally (or via the
    strategies/dependencies modules) and return the disabled variant with the
    correct ``feature_enabled`` flag when no variant applies. Returns ``None``
    only when the feature is unknown (so the facade can signal "unknown toggle").

    :param feature: The compiled feature.
    :param context: The evaluation context.
    :param state: The full engine state.
    :return: The selected :class:`Variant`, or ``None`` for unknown features.
    """
    raise NotImplementedError
