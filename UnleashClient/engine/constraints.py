"""Constraint operator evaluation for the embedded engine.

Implements every Unleash constraint operator against a (pre-compiled)
``ConstraintDef`` and a flat evaluation ``context`` dict:

* List:    ``IN``, ``NOT_IN``
* String:  ``STR_CONTAINS``, ``STR_STARTS_WITH``, ``STR_ENDS_WITH``
* Numeric: ``NUM_EQ``, ``NUM_GT``, ``NUM_GTE``, ``NUM_LT``, ``NUM_LTE``
* Date:    ``DATE_AFTER``, ``DATE_BEFORE``
* Semver:  ``SEMVER_EQ``, ``SEMVER_GT``, ``SEMVER_LT``

The ``caseInsensitive`` and ``inverted`` flags are honored. Constraint-side
values are pre-parsed at state-load time (see ``state.ConstraintDef``); this
module performs only the per-call context-side parsing and comparison.

Context field resolution looks up ``context_name`` across top-level context
fields first, then ``context["properties"]``.
"""

from __future__ import annotations

from typing import Any, Optional

from UnleashClient.engine.state import ConstraintDef


def get_context_value(context: dict, context_name: str) -> Optional[Any]:
    """Resolve a context field by name.

    Looks up ``context_name`` among the top-level context keys, falling back to
    ``context["properties"]``. Returns ``None`` when absent.

    :param context: The evaluation context.
    :param context_name: The constraint's ``contextName``.
    :return: The resolved value, or ``None``.
    """
    raise NotImplementedError


def evaluate_constraint(constraint: ConstraintDef, context: dict) -> bool:
    """Evaluate a single constraint against the context.

    Applies the operator-specific comparison, then the ``inverted`` flag. A
    missing context value or an unparseable context-side value (for numeric /
    semver / date operators) yields the operator's "no match" result **before**
    inversion — the exact ordering is pinned by the client-specification suite.

    :param constraint: The pre-compiled constraint.
    :param context: The evaluation context.
    :return: ``True`` if the constraint is satisfied, else ``False``.
    """
    raise NotImplementedError
