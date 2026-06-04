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

Performance notes (design spec): nothing here re-parses JSON or compiles regex.
The ``ipaddress`` objects for ``remoteAddress`` are derived from the raw
``IPs`` parameter string on the hot path; this is the one place a strategy does
per-call string work, kept minimal and exception-tolerant. State pre-compilation
lives in :mod:`UnleashClient.engine.state`; constraint values are pre-parsed
there and consumed via the frozen :func:`constraints.evaluate_constraint`.
"""

from __future__ import annotations

import ipaddress
import platform
import random
from typing import Callable, Dict, Optional

# These two are pulled into the module namespace (rather than referenced via
# their package) so unit tests can monkeypatch them while the sibling modules
# are implemented in parallel, and so the hot path avoids attribute lookups
# across module boundaries.
from UnleashClient.engine.constraints import evaluate_constraint, get_context_value
from UnleashClient.engine.hashing import normalized_hash
from UnleashClient.engine.state import EngineState, StrategyDef

# ---------------------------------------------------------------------------
# default
# ---------------------------------------------------------------------------


def apply_default(strategy: StrategyDef, context: dict, state: EngineState) -> bool:
    """The ``default`` strategy: always on (once constraints pass)."""
    return True


# ---------------------------------------------------------------------------
# userWithId
# ---------------------------------------------------------------------------


def apply_user_with_id(
    strategy: StrategyDef, context: dict, state: EngineState
) -> bool:
    """The ``userWithId`` strategy: enabled when ``userId`` is in ``userIds``.

    ``userIds`` is a comma-separated string; each entry is trimmed
    (spec ``02-user-with-id-strategy`` Feature.D2). A missing ``userId`` on the
    context, or an empty / missing ``userIds`` parameter, is a non-match.
    """
    user_id = context.get("userId")
    if user_id is None:
        return False

    # Prefer the set pre-computed at load time (single O(1) lookup). Fall back to
    # splitting here only for a ``StrategyDef`` built directly (e.g. in a test).
    user_ids = strategy.parsed_user_ids
    if user_ids is None:
        raw = strategy.parameters.get("userIds")
        if raw is None:
            return False
        # ``"".split(",")`` yields ``[""]``, a matchable empty token: a context
        # ``userId`` of ``""`` matches a ``userIds`` of ``""`` (native parity).
        user_ids = frozenset(x.strip() for x in raw.split(","))
    return user_id in user_ids


# ---------------------------------------------------------------------------
# gradualRolloutUserId / gradualRolloutSessionId (legacy)
# ---------------------------------------------------------------------------


def apply_gradual_rollout_user_id(
    strategy: StrategyDef, context: dict, state: EngineState
) -> bool:
    """Legacy ``gradualRolloutUserId``: percentage rollout keyed on ``userId``."""
    user_id = context.get("userId")
    if user_id is None:
        return False
    return _rollout_match(
        strategy.parameters.get("percentage"),
        strategy.parameters.get("groupId", ""),
        user_id,
    )


def apply_gradual_rollout_session_id(
    strategy: StrategyDef, context: dict, state: EngineState
) -> bool:
    """Legacy ``gradualRolloutSessionId``: percentage rollout keyed on ``sessionId``."""
    session_id = context.get("sessionId")
    if session_id is None:
        return False
    return _rollout_match(
        strategy.parameters.get("percentage"),
        strategy.parameters.get("groupId", ""),
        session_id,
    )


# ---------------------------------------------------------------------------
# gradualRolloutRandom
# ---------------------------------------------------------------------------


def apply_gradual_rollout_random(
    strategy: StrategyDef, context: dict, state: EngineState
) -> bool:
    """Legacy ``gradualRolloutRandom``: random percentage rollout."""
    percentage = _to_int(strategy.parameters.get("percentage"))
    return percentage > 0 and random.randint(1, 100) <= percentage


# ---------------------------------------------------------------------------
# flexibleRollout
# ---------------------------------------------------------------------------


def apply_flexible_rollout(
    strategy: StrategyDef, context: dict, state: EngineState
) -> bool:
    """The ``flexibleRollout`` strategy.

    Honors ``rollout``, ``groupId`` (defaults to empty string when absent —
    the SDK supplies the feature name) and ``stickiness``:

    * ``default`` -> ``userId``, else ``sessionId``, else a random hash;
    * ``random``  -> a random hash;
    * any other value is a custom context field name -> resolved via
      :func:`constraints.get_context_value` (top-level then ``properties``). A
      missing value disables the strategy (matching the native engine: the empty
      identifier is *not* hashed — absent stickiness means no rollout).

    Note ``userId`` / ``sessionId`` are not special keywords here; they fall
    through the custom-field branch and resolve as ordinary context fields.

    Uses the normalized murmur3 hash with the default rollout seed (``0``).
    """
    percentage = _to_int(strategy.parameters.get("rollout"))
    if percentage <= 0:
        return False

    group_id = strategy.parameters.get("groupId", "")
    stickiness = strategy.parameters.get("stickiness") or "default"

    # Resolve the sticky identifier BEFORE any 100% short-circuit: a named
    # stickiness field (custom, or explicit userId/sessionId) that is missing
    # from the context disables the strategy at ANY rollout — the native
    # engine never hashes an absent identifier. Only "default" and "random"
    # may fall back to a random assignment.
    if stickiness == "default":
        if "userId" in context:
            identifier = context["userId"]
        elif "sessionId" in context:
            identifier = context["sessionId"]
        else:
            # No sticky identifier on context -> random assignment.
            return random.randint(1, 100) <= percentage
    elif stickiness == "random":
        return random.randint(1, 100) <= percentage
    else:
        # Named stickiness field. A missing value disables the strategy
        # entirely (the native engine does not hash an absent identifier).
        value = get_context_value(context, stickiness)
        if value is None:
            return False
        identifier = value

    if percentage >= 100:
        # 100% is always on once a sticky identifier resolved.
        return True
    return normalized_hash(group_id, identifier) <= percentage


# ---------------------------------------------------------------------------
# remoteAddress
# ---------------------------------------------------------------------------


def apply_remote_address(
    strategy: StrategyDef, context: dict, state: EngineState
) -> bool:
    """The ``remoteAddress`` strategy: matches single IPs and CIDR ranges.

    Uses stdlib ``ipaddress``; invalid addresses (config or context side) are
    treated as non-matches rather than raising. IPv4/IPv6 version mismatches are
    non-matches.
    """
    raw_ip = context.get("remoteAddress")
    if raw_ip is None:
        return False

    try:
        context_ip = ipaddress.ip_address(raw_ip)
    except ValueError:
        return False

    # Prefer the networks/addresses pre-parsed at load time (mirrors the IN_CIDR
    # constraint's ``parsed_cidrs``). Fall back to parsing the raw ``IPs`` string
    # only for a ``StrategyDef`` built directly (e.g. in a test).
    networks = strategy.parsed_ips
    if networks is None:
        from UnleashClient.engine.state import (  # noqa: PLC0415
            _compile_strategy_ips,
        )

        networks = _compile_strategy_ips(strategy.parameters.get("IPs"))

    for entry in networks:
        if isinstance(entry, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
            if entry.version == context_ip.version and context_ip in entry:
                return True
        elif entry == context_ip:
            return True

    return False


# ---------------------------------------------------------------------------
# applicationHostname
# ---------------------------------------------------------------------------


def apply_application_hostname(
    strategy: StrategyDef, context: dict, state: EngineState
) -> bool:
    """The ``applicationHostname`` strategy: matches the host's name."""
    host_names = strategy.parsed_host_names
    if host_names is None:
        raw = strategy.parameters.get("hostNames")
        if not raw:
            return False
        host_names = frozenset(x.strip() for x in raw.split(","))
    return platform.node() in host_names


# ---------------------------------------------------------------------------
# Dispatch + constraint/segment gate
# ---------------------------------------------------------------------------


#: Dispatch table mapping strategy name -> evaluator.
STRATEGY_DISPATCH: Dict[str, Callable[[StrategyDef, dict, EngineState], bool]] = {
    "default": apply_default,
    "userWithId": apply_user_with_id,
    "gradualRolloutUserId": apply_gradual_rollout_user_id,
    "gradualRolloutSessionId": apply_gradual_rollout_session_id,
    "gradualRolloutRandom": apply_gradual_rollout_random,
    "flexibleRollout": apply_flexible_rollout,
    "remoteAddress": apply_remote_address,
    "applicationHostname": apply_application_hostname,
}


def evaluate_strategy(
    strategy: StrategyDef,
    context: dict,
    state: EngineState,
) -> bool:
    """Evaluate a single strategy (constraints + segments + strategy logic).

    Returns ``False`` immediately if the strategy references a missing segment
    (``has_missing_segment``) or any constraint fails; otherwise dispatches to
    the named built-in strategy. An unknown strategy name evaluates to ``False``.

    Constraints carry the strategy's own constraints plus every resolved
    segment's constraints (flattened at load time by :mod:`state`), so a single
    AND-scan here covers both.

    :param strategy: The pre-compiled strategy.
    :param context: The evaluation context.
    :param state: The full engine state (for segment / cross-references).
    :return: ``True`` if the strategy grants access, else ``False``.
    """
    # A strategy referencing a non-existent segment can never pass
    # (spec 15-global-constraints).
    if strategy.has_missing_segment:
        return False

    apply_fn = STRATEGY_DISPATCH.get(strategy.name)
    if apply_fn is None:
        # Unknown / unsupported (e.g. custom) strategy: never grants access.
        return False

    # All constraints (strategy + resolved segments) must pass (AND semantics).
    # ``evaluate_constraint`` is resolved via the module global so tests can
    # monkeypatch it while ``constraints`` is implemented in parallel.
    for constraint in strategy.constraints:
        if not evaluate_constraint(constraint, context):
            return False

    return apply_fn(strategy, context, state)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _to_int(value: Optional[str]) -> int:
    """Parse a percentage/rollout parameter to ``int``; ``0`` when invalid."""
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _rollout_match(percentage: Optional[str], group_id: str, identifier: str) -> bool:
    """Shared gradual-rollout comparison: ``hash <= percentage`` (percentage>0)."""
    pct = _to_int(percentage)
    if pct <= 0:
        return False
    if pct >= 100:
        return True
    return normalized_hash(group_id, identifier) <= pct
