"""Built-in strategy evaluation for the embedded engine.

Each built-in strategy is a pure function ``(strategy, context, state) -> bool``.
``evaluate_strategy`` dispatches on ``strategy.name`` and first verifies that
the strategy's (segment-resolved) constraints all pass before applying the
strategy logic. A strategy that references a non-existent segment evaluates to
``False`` (spec ``15-global-constraints``); ``state.StrategyDef.has_missing_segment``
carries that signal.

Built-in strategies:

* ``default``
* ``userWithId``
* ``gradualRolloutUserId``
* ``gradualRolloutSessionId``
* ``gradualRolloutRandom``
* ``flexibleRollout``
* ``remoteAddress``
* ``applicationHostname``

Custom strategies are unsupported by the embedded engine (see the facade's
``register_custom_strategies``).
"""

from __future__ import annotations

from typing import Callable, Dict

from UnleashClient.engine.state import EngineState, StrategyDef


def evaluate_strategy(
    strategy: StrategyDef,
    context: dict,
    state: EngineState,
) -> bool:
    """Evaluate a single strategy (constraints + segments + strategy logic).

    Returns ``False`` immediately if the strategy references a missing segment
    or any constraint fails; otherwise dispatches to the named built-in
    strategy. An unknown strategy name evaluates to ``False``.

    :param strategy: The pre-compiled strategy.
    :param context: The evaluation context.
    :param state: The full engine state (for segment / cross-references).
    :return: ``True`` if the strategy grants access, else ``False``.
    """
    raise NotImplementedError


def apply_default(strategy: StrategyDef, context: dict, state: EngineState) -> bool:
    """The ``default`` strategy: always on (once constraints pass)."""
    raise NotImplementedError


def apply_user_with_id(strategy: StrategyDef, context: dict, state: EngineState) -> bool:
    """The ``userWithId`` strategy: enabled when ``userId`` is in ``userIds``."""
    raise NotImplementedError


def apply_gradual_rollout_user_id(
    strategy: StrategyDef, context: dict, state: EngineState
) -> bool:
    """Legacy ``gradualRolloutUserId``: percentage rollout keyed on ``userId``."""
    raise NotImplementedError


def apply_gradual_rollout_session_id(
    strategy: StrategyDef, context: dict, state: EngineState
) -> bool:
    """Legacy ``gradualRolloutSessionId``: percentage rollout keyed on ``sessionId``."""
    raise NotImplementedError


def apply_gradual_rollout_random(
    strategy: StrategyDef, context: dict, state: EngineState
) -> bool:
    """Legacy ``gradualRolloutRandom``: random percentage rollout."""
    raise NotImplementedError


def apply_flexible_rollout(
    strategy: StrategyDef, context: dict, state: EngineState
) -> bool:
    """The ``flexibleRollout`` strategy.

    Honors ``rollout``, ``groupId`` (defaults to feature name when absent) and
    ``stickiness`` (``default`` -> userId then sessionId then random fallback;
    ``random``; or a custom context field). Uses the normalized murmur3 hash
    with the default rollout seed.
    """
    raise NotImplementedError


def apply_remote_address(
    strategy: StrategyDef, context: dict, state: EngineState
) -> bool:
    """The ``remoteAddress`` strategy: matches single IPs and CIDR ranges.

    Uses stdlib ``ipaddress``; invalid addresses (config or context side) are
    treated as non-matches rather than raising.
    """
    raise NotImplementedError


def apply_application_hostname(
    strategy: StrategyDef, context: dict, state: EngineState
) -> bool:
    """The ``applicationHostname`` strategy: matches the host's name."""
    raise NotImplementedError


#: Dispatch table mapping strategy name -> evaluator. Populated by the
#: implementer; ``evaluate_strategy`` uses it (or an equivalent dispatch).
STRATEGY_DISPATCH: Dict[
    str, Callable[[StrategyDef, dict, EngineState], bool]
] = {}
