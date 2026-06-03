"""Dependent-feature resolution for the embedded engine.

Implements spec ``14-dependent-features``: a feature may declare dependencies on
parent features. A dependency is satisfied when:

* the parent feature exists, and
* the parent's enabled state matches the dependency's expected ``enabled`` flag
  (default ``True``), and
* if the dependency lists required ``variants``, the parent's selected variant
  name is among them.

A missing parent makes the dependency (and therefore the dependent feature)
evaluate to ``False``. Dependencies are not transitive: a parent that is itself
dependent on a grandparent is treated as a leaf for this check (the spec pins
this; verify against the suite).
"""

from __future__ import annotations

from UnleashClient.engine.state import EngineState, FeatureState


def dependencies_satisfied(
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
    raise NotImplementedError
