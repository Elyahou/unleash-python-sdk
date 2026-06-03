"""Constraint operator evaluation for the embedded engine.

Implements every Unleash constraint operator against a (pre-compiled)
``ConstraintDef`` and a flat evaluation ``context`` dict:

* List:    ``IN``, ``NOT_IN``
* String:  ``STR_CONTAINS``, ``STR_STARTS_WITH``, ``STR_ENDS_WITH``
* Numeric: ``NUM_EQ``, ``NUM_GT``, ``NUM_GTE``, ``NUM_LT``, ``NUM_LTE``
* Date:    ``DATE_AFTER``, ``DATE_BEFORE``
* Semver:  ``SEMVER_EQ``, ``SEMVER_GT``, ``SEMVER_GTE``, ``SEMVER_LT``,
  ``SEMVER_LTE``
* Regex:   ``REGEX``
* CIDR:    ``IN_CIDR``

The ``caseInsensitive`` and ``inverted`` flags are honored. Constraint-side
values are pre-parsed at state-load time (see ``state.ConstraintDef``) wherever
the frozen interface exposes a ``parsed_*`` field; this module uses those cached
representations on the hot path and falls back to parsing ``value`` / folding
``values`` itself when they are absent (so the module is self-contained).

The ``REGEX`` and ``IN_CIDR`` operators are pre-compiled at state-load time into
``ConstraintDef.parsed_regex`` (a compiled ``re.Pattern`` or ``None`` when the
pattern is invalid / RE2-incompatible) and ``ConstraintDef.parsed_cidrs`` (a list
of parsed ``ip_network`` objects). This module reads those cached forms on the
hot path and falls back to compiling them itself only if the state layer left
them unset (so the module remains self-contained).

Inversion semantics: the operator-specific comparison produces a raw boolean
("did the constraint match?"). For every *known* operator the ``inverted`` flag
flips the final result — this includes the missing-context and unparseable
cases, matching the Unleash client specification (e.g. spec 13's
``F8.missing-field.NOT_IN`` / ``F9`` / ``F10`` and the inverted numeric cases).
An *unknown* operator never matches and is never inverted (spec 13
``F7.invalid-operator``).

Context field resolution looks up ``context_name`` across top-level context
fields first, then ``context["properties"]``.
"""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime, timezone
from typing import Any, List, Optional

import semver

from UnleashClient.engine.state import ConstraintDef

# ---------------------------------------------------------------------------
# Operator groupings (module-level sets -> O(1) membership on the hot path)
# ---------------------------------------------------------------------------

_LIST_OPS = frozenset({"IN", "NOT_IN"})
_STRING_OPS = frozenset({"STR_CONTAINS", "STR_STARTS_WITH", "STR_ENDS_WITH"})
_NUM_OPS = frozenset({"NUM_EQ", "NUM_GT", "NUM_GTE", "NUM_LT", "NUM_LTE"})
_DATE_OPS = frozenset({"DATE_AFTER", "DATE_BEFORE"})
_SEMVER_OPS = frozenset(
    {"SEMVER_EQ", "SEMVER_GT", "SEMVER_GTE", "SEMVER_LT", "SEMVER_LTE"}
)

# Sentinel cached on a constraint instance when its REGEX / IN_CIDR value could
# not be compiled (distinguishes "unparseable" from "not yet compiled").
_INVALID = object()

# RE2 (the engine the spec oracle uses) rejects look-around and backreferences.
# Python's ``re`` accepts them, so we must detect and reject them to stay
# spec-compatible (spec 21 ``R90`` / ``R91``).
_RE2_UNSUPPORTED = re.compile(
    r"\(\?="  # lookahead
    r"|\(\?!"  # negative lookahead
    r"|\(\?<="  # lookbehind
    r"|\(\?<!"  # negative lookbehind
    r"|\\[1-9]"  # numeric backreference
)

# POSIX character classes (RE2 supports them; Python ``re`` does not). Mapped to
# their ASCII equivalents — these only appear *inside* a ``[...]`` set, so the
# replacement is the bare class body without surrounding brackets.
_POSIX_CLASSES = {
    "[:alpha:]": "a-zA-Z",
    "[:digit:]": "0-9",
    "[:alnum:]": "a-zA-Z0-9",
    "[:upper:]": "A-Z",
    "[:lower:]": "a-z",
    "[:space:]": r"\s",
    "[:blank:]": r" \t",
    "[:punct:]": r"!-/:-@\[-`{-~",
    "[:word:]": r"\w",
    "[:xdigit:]": "0-9a-fA-F",
    "[:cntrl:]": r"\x00-\x1f\x7f",
    "[:graph:]": r"\x21-\x7e",
    "[:print:]": r"\x20-\x7e",
}
_POSIX_CLASS_RE = re.compile(r"\[:\^?[a-z]+:\]")

# Bare inline global-flag group, e.g. ``(?i)`` / ``(?ms)`` (no ``:``). In RE2
# these are global to the whole expression regardless of position, but Python
# 3.11+ rejects them unless they sit at the very start. We lift any such groups
# to the front to recover RE2's global semantics.
_INLINE_GLOBAL_FLAGS = re.compile(r"\(\?([aiLmsux]+)\)")


# ---------------------------------------------------------------------------
# Context resolution
# ---------------------------------------------------------------------------


def get_context_value(context: dict, context_name: str) -> Optional[Any]:
    """Resolve a context field by name.

    Looks up ``context_name`` among the top-level context keys, falling back to
    ``context["properties"]``. Returns ``None`` when absent (a property present
    but explicitly ``null`` is treated as absent, per spec 21/22).

    :param context: The evaluation context.
    :param context_name: The constraint's ``contextName``.
    :return: The resolved value, or ``None``.
    """
    if context_name in context:
        return context[context_name]
    properties = context.get("properties")
    if properties is not None and context_name in properties:
        return properties[context_name]
    return None


# ---------------------------------------------------------------------------
# Per-operator comparisons (raw "did it match?" — inversion applied by caller)
# ---------------------------------------------------------------------------


def _match_list(constraint: ConstraintDef, context_value: Any) -> bool:
    if constraint.operator == "IN":
        return context_value in constraint.values
    # NOT_IN
    return context_value not in constraint.values


def _match_string(constraint: ConstraintDef, context_value: Any) -> bool:
    if not isinstance(context_value, str):
        context_value = str(context_value)

    if constraint.case_insensitive:
        # Prefer the state-compiled fold; fall back to folding here.
        if constraint.parsed_values is not None:
            values = constraint.parsed_values
        else:
            values = [v.upper() for v in constraint.values]
        ctx = context_value.upper()
    else:
        values = constraint.values
        ctx = context_value

    op = constraint.operator
    if op == "STR_CONTAINS":
        return any(v in ctx for v in values)
    if op == "STR_STARTS_WITH":
        return any(ctx.startswith(v) for v in values)
    # STR_ENDS_WITH
    return any(ctx.endswith(v) for v in values)


# Rust's ``f64::from_str`` grammar (used by yggdrasil for NUM_* context values):
# optional sign, then either ``inf``/``infinity``/``nan`` (case-insensitive) or a
# decimal number requiring at least one digit on at least one side of the dot,
# with an optional exponent. Crucially it rejects surrounding whitespace,
# underscores, a bare leading dot (``.5``), thousands separators and non-ASCII
# digits — all of which Python's ``float()`` would accept and silently diverge.
_RUST_F64_RE = re.compile(
    r"[+-]?(?:inf(?:inity)?|nan|(?:[0-9]+(?:\.[0-9]*)?|[0-9]*\.[0-9]+)(?:[eE][+-]?[0-9]+)?)\Z",
    re.IGNORECASE,
)


def _rust_parse_f64(value: Any) -> Optional[float]:
    """Parse a context value the way Rust's ``f64::from_str`` does.

    Returns ``None`` when Rust would reject the input, so the embedded engine
    matches yggdrasil for whitespace-/underscore-padded or otherwise non-Rust
    numeric strings instead of silently coercing them via Python ``float()``.
    Real ``int``/``float`` values are accepted directly.
    """
    if isinstance(value, bool):
        # bool is a subclass of int but is never a numeric context value.
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    # ``[0-9]`` (not ``\d``) and the explicit grammar exclude underscores,
    # surrounding whitespace and non-ASCII digits that ``float()`` would accept.
    if not _RUST_F64_RE.match(value):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _match_numeric(  # noqa: PLR0911
    constraint: ConstraintDef, context_value: Any
) -> bool:
    target = constraint.parsed_number
    if target is None:
        try:
            target = float(constraint.value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False

    ctx = _rust_parse_f64(context_value)
    if ctx is None:
        return False

    op = constraint.operator
    if op == "NUM_EQ":
        return ctx == target
    if op == "NUM_GT":
        return ctx > target
    if op == "NUM_GTE":
        return ctx >= target
    if op == "NUM_LT":
        return ctx < target
    # NUM_LTE
    return ctx <= target


def _to_aware_datetime(value: Any) -> Optional[datetime]:
    """Parse ``value`` into a timezone-aware datetime, RFC3339-strict.

    The native engine (chrono ``DateTime::parse_from_rfc3339``) only accepts a
    full RFC3339 timestamp: a ``T``-separated date and time *with* an explicit
    timezone offset (``Z`` or ``±hh:mm``). Anything looser — a date-only string,
    a space separator, a missing offset, or non-date garbage such as ``"42.57"``
    / ``"15"`` — is a non-match. We mirror that strictness (a permissive parser
    like ``dateutil`` would coerce ``"42.57"`` into a year and diverge).

    A real ``datetime`` instance (e.g. the substituted ``currentTime`` "now") is
    accepted as-is, with a naive value assumed to be UTC.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        # Require an explicit timezone: chrono's RFC3339 parser rejects naive
        # timestamps. ``fromisoformat`` accepts date-only and space-separated
        # forms that chrono refuses, so we additionally require a ``T``/``t``
        # separator and a resulting non-None tzinfo.
        if "T" not in value and "t" not in value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
    else:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _match_date(constraint: ConstraintDef, context_value: Any) -> bool:
    target = constraint.parsed_date
    if target is None:
        target = _to_aware_datetime(constraint.value)
        if target is None:
            return False
    elif target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)

    ctx = _to_aware_datetime(context_value)
    if ctx is None:
        return False

    if constraint.operator == "DATE_AFTER":
        return ctx > target
    # DATE_BEFORE
    return ctx < target


def _parse_semver(value: Any) -> Optional[semver.Version]:
    if isinstance(value, semver.Version):
        return value
    try:
        return semver.Version.parse(value)
    except (ValueError, TypeError):
        return None


def _compare_build_identifiers(a: str, b: str) -> int:  # noqa: PLR0911
    """Compare two build-metadata strings the way Rust's ``semver`` crate does.

    The Rust crate (yggdrasil's oracle) participates build metadata in ordering,
    unlike SemVer §10 / python-semver which ignore it. It applies the §11
    prerelease comparison rules to the dot-separated build identifiers:

    * a non-empty build sorts **above** an empty build;
    * identifiers are compared left to right;
    * purely-numeric identifiers compare numerically and rank **below**
      alphanumeric identifiers; alphanumeric identifiers compare ASCII-lexically;
    * when all shared identifiers are equal, the longer identifier list is
      greater.

    Returns ``-1`` / ``0`` / ``1`` for ``a`` <, ==, > ``b``.
    """
    if a == b:
        return 0
    # Empty build sorts below any non-empty build.
    if not a:
        return -1
    if not b:
        return 1

    a_parts = a.split(".")
    b_parts = b.split(".")
    for ap, bp in zip(a_parts, b_parts):
        if ap == bp:
            continue
        a_num = ap.isdigit() and (ap == "0" or not ap.startswith("0"))
        b_num = bp.isdigit() and (bp == "0" or not bp.startswith("0"))
        if a_num and b_num:
            ai, bi = int(ap), int(bp)
            if ai != bi:
                return -1 if ai < bi else 1
        elif a_num != b_num:
            # Numeric identifiers always have lower precedence than alphanumeric.
            return -1 if a_num else 1
        else:
            return -1 if ap < bp else 1
    # All shared identifiers equal: the longer list is greater.
    if len(a_parts) != len(b_parts):
        return -1 if len(a_parts) < len(b_parts) else 1
    return 0


def _compare_semver(ctx: semver.Version, target: semver.Version) -> int:
    """Three-way semver comparison matching yggdrasil's Rust ``semver`` crate.

    python-semver's own comparison correctly orders major/minor/patch and
    prerelease but *ignores* build metadata. The Rust crate orders build
    metadata too, so when the core comparison is equal we break the tie on the
    build metadata via :func:`_compare_build_identifiers`.
    """
    core = ctx.compare(target)
    if core != 0:
        return core
    return _compare_build_identifiers(ctx.build or "", target.build or "")


def _match_semver(  # noqa: PLR0911
    constraint: ConstraintDef, context_value: Any
) -> bool:
    target = constraint.parsed_semver
    if target is None:
        target = _parse_semver(constraint.value)
        if target is None:
            return False

    ctx = _parse_semver(context_value)
    if ctx is None:
        return False

    cmp = _compare_semver(ctx, target)

    op = constraint.operator
    if op == "SEMVER_EQ":
        return cmp == 0
    if op == "SEMVER_GT":
        return cmp > 0
    if op == "SEMVER_GTE":
        return cmp >= 0
    if op == "SEMVER_LT":
        return cmp < 0
    # SEMVER_LTE
    return cmp <= 0


def _translate_posix_classes(pattern: str) -> str:
    """Rewrite POSIX ``[[:class:]]`` constructs to Python-compatible classes.

    RE2 supports POSIX classes; Python's ``re`` parses ``[[:alpha:]]`` as a
    nested set and would mismatch. An unknown class is left untouched (the
    compile will then fail and the regex is treated as invalid, matching RE2's
    rejection of unknown classes).
    """
    if "[:" not in pattern:
        return pattern

    def _sub(m: "re.Match[str]") -> str:
        token = m.group(0)
        return _POSIX_CLASSES.get(token, token)

    return _POSIX_CLASS_RE.sub(_sub, pattern)


def _hoist_inline_flags(pattern: str) -> str:
    """Move bare global inline flags (``(?i)``-style) to the front of ``pattern``.

    RE2 treats such groups as global regardless of position; Python 3.11+ only
    accepts them at the start. Scoped groups (``(?i:...)``) are untouched.
    """
    flags = ""
    matches = list(_INLINE_GLOBAL_FLAGS.finditer(pattern))
    if not matches:
        return pattern
    for m in matches:
        for ch in m.group(1):
            if ch not in flags:
                flags += ch
    stripped = _INLINE_GLOBAL_FLAGS.sub("", pattern)
    return f"(?{flags}){stripped}"


def _compiled_regex(constraint: ConstraintDef) -> Any:
    """Return the pre-compiled regex, or ``_INVALID`` when the pattern is bad.

    Prefers ``ConstraintDef.parsed_regex`` (pre-compiled by ``state.py`` at load
    time). Falls back to compiling here only if the state layer left it unset
    (e.g. a constraint constructed directly in a test), caching the result on the
    instance so the hot path never recompiles.
    """
    if constraint.parsed_regex is not None:
        return constraint.parsed_regex

    cached = getattr(constraint, "_compiled_regex_cache", None)
    if cached is not None:
        return cached

    pattern = constraint.value
    compiled: Any
    if not isinstance(pattern, str) or _RE2_UNSUPPORTED.search(pattern):
        compiled = _INVALID
    else:
        pattern = _hoist_inline_flags(_translate_posix_classes(pattern))
        flags = re.IGNORECASE if constraint.case_insensitive else 0
        try:
            compiled = re.compile(pattern, flags)
        except re.error:
            compiled = _INVALID

    # Cache on the instance so the hot path never recompiles.
    object.__setattr__(constraint, "_compiled_regex_cache", compiled)
    return compiled


def _match_regex(constraint: ConstraintDef, context_value: Any) -> bool:
    compiled = _compiled_regex(constraint)
    if compiled is _INVALID:
        return False
    if not isinstance(context_value, str):
        return False
    return compiled.search(context_value) is not None


def _compiled_cidrs(constraint: ConstraintDef) -> List[Any]:
    """Return the parsed networks, preferring the state-pre-compiled list."""
    if constraint.parsed_cidrs is not None:
        return constraint.parsed_cidrs

    cached = getattr(constraint, "_compiled_cidrs_cache", None)
    if cached is not None:
        return cached

    networks = []
    for raw in constraint.values:
        try:
            networks.append(ipaddress.ip_network(raw, strict=False))
        except ValueError:
            continue  # invalid entries are ignored

    object.__setattr__(constraint, "_compiled_cidrs_cache", networks)
    return networks


def _match_cidr(constraint: ConstraintDef, context_value: Any) -> bool:
    networks = _compiled_cidrs(constraint)
    if not networks:
        return False
    try:
        ip = ipaddress.ip_address(context_value)
    except (ValueError, TypeError):
        return False
    return any(ip in network for network in networks)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def evaluate_constraint(  # noqa: PLR0912
    constraint: ConstraintDef, context: dict
) -> bool:
    """Evaluate a single constraint against the context.

    Applies the operator-specific comparison, then the ``inverted`` flag. A
    missing context value or an unparseable context-side value (for numeric /
    semver / date / regex / cidr operators) yields the operator's "no match"
    result **before** inversion. An unknown operator always evaluates ``False``
    and is never inverted.

    :param constraint: The pre-compiled constraint.
    :param context: The evaluation context.
    :return: ``True`` if the constraint is satisfied, else ``False``.
    """
    operator = constraint.operator

    context_value = get_context_value(context, constraint.context_name)

    # "currentTime" is special: when absent the engine substitutes "now".
    if (
        context_value is None
        and constraint.context_name == "currentTime"
        and operator in _DATE_OPS
    ):
        context_value = datetime.now(timezone.utc)

    if context_value is None:
        # Missing context: only NOT_IN matches (spec 13 F8). Everything else is
        # a non-match. The inverted flag still flips the result for known ops.
        if operator == "NOT_IN":
            matched = True
        elif (
            operator in _LIST_OPS
            or operator in _STRING_OPS
            or operator in _NUM_OPS
            or operator in _DATE_OPS
            or operator in _SEMVER_OPS
            or operator in ("REGEX", "IN_CIDR")
        ):
            matched = False
        else:
            return False  # unknown operator: never matched, never inverted
        return (not matched) if constraint.inverted else matched

    if operator in _LIST_OPS:
        matched = _match_list(constraint, context_value)
    elif operator in _STRING_OPS:
        matched = _match_string(constraint, context_value)
    elif operator in _NUM_OPS:
        matched = _match_numeric(constraint, context_value)
    elif operator in _DATE_OPS:
        matched = _match_date(constraint, context_value)
    elif operator in _SEMVER_OPS:
        matched = _match_semver(constraint, context_value)
    elif operator == "REGEX":
        matched = _match_regex(constraint, context_value)
    elif operator == "IN_CIDR":
        matched = _match_cidr(constraint, context_value)
    else:
        return False  # unknown operator: never matched, never inverted

    return (not matched) if constraint.inverted else matched
