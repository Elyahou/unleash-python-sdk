"""Internal, pre-compiled state model for the embedded engine.

This module owns the parsing of a raw Unleash ``ClientFeatures`` payload into
an in-memory model that is optimized for the evaluation hot path. Per the
design spec's performance section, all expensive work happens **once** at
``take_state`` time:

* constraint values are pre-parsed (semver -> ``Version``, dates -> aware
  ``datetime``, numerics -> ``float``);
* segment references are pre-resolved so strategies carry their effective
  constraint list directly;
* variant weight tables are precomputed (running cumulative weights), so
  variant selection is a single hash + scan.

The evaluation modules (``constraints``, ``strategies``, ``variants``,
``dependencies``) consume these dataclasses and MUST NOT re-parse raw JSON on
the hot path.

Interfaces here are FROZEN for the parallel implementers.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Pattern, Tuple

try:  # pragma: no cover - import shim
    from semver import VersionInfo as _SemverVersion
except ImportError:  # pragma: no cover
    from semver.version import Version as _SemverVersion  # type: ignore

from dateutil.parser import parse as _parse_date

# ---------------------------------------------------------------------------
# Constraint model
# ---------------------------------------------------------------------------


@dataclass
class ConstraintDef:
    """A single, pre-compiled constraint.

    The raw values from the payload are retained (``values`` / ``value``) for
    serialization round-tripping, while the ``parsed_*`` fields hold the
    pre-computed representations used on the evaluation hot path. Which
    ``parsed_*`` field is populated depends on ``operator``:

    * ``NUM_*``  -> ``parsed_number`` (float of ``value``)
    * ``SEMVER_*`` -> ``parsed_semver`` (parsed semantic version of ``value``)
    * ``DATE_*`` -> ``parsed_date`` (timezone-aware datetime of ``value``)
    * ``IN`` / ``NOT_IN`` / ``STR_*`` -> ``values`` (optionally case-folded into
      ``parsed_values`` when ``case_insensitive`` is set)
    * ``REGEX`` -> ``parsed_regex`` (compiled ``re.Pattern``)
    * ``IN_CIDR`` -> ``parsed_cidrs`` (list of parsed ``ip_network`` objects)

    A ``parsed_*`` field left as ``None`` after parsing signals that the
    constraint's own value was unparseable; ``constraints.evaluate_constraint``
    defines the resulting truth value (and how ``inverted`` interacts). For
    ``parsed_regex`` specifically, ``None`` means the pattern was invalid /
    RE2-incompatible and the constraint can never match.
    """

    context_name: str
    operator: str
    values: List[str] = field(default_factory=list)
    value: Optional[str] = None
    case_insensitive: bool = False
    inverted: bool = False

    # Pre-compiled, operator-specific representations (populated by parsing).
    parsed_number: Optional[float] = None
    parsed_semver: Optional[Any] = (
        None  # semver.Version (kept Any to avoid import here)
    )
    parsed_date: Optional[datetime] = None
    parsed_values: Optional[List[str]] = None  # case-folded copy of ``values``
    parsed_regex: Optional[Pattern] = None  # compiled REGEX pattern
    parsed_cidrs: Optional[List[Any]] = None  # list of ip_network for IN_CIDR


# ---------------------------------------------------------------------------
# Segment model
# ---------------------------------------------------------------------------


@dataclass
class Segment:
    """A reusable, named set of constraints referenced by strategies."""

    segment_id: int
    constraints: List[ConstraintDef] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Variant model
# ---------------------------------------------------------------------------


@dataclass
class VariantOverride:
    """A variant override: forces a variant when a context field matches."""

    context_name: str
    values: List[str] = field(default_factory=list)


@dataclass
class VariantDef:
    """A pre-compiled variant definition with its cumulative weight boundary.

    ``cumulative_weight`` is the running total of weights up to and including
    this variant, precomputed at load time so selection is a single scan:
    pick the first variant whose ``cumulative_weight >= target``.
    """

    name: str
    weight: int
    payload: Optional[Dict[str, str]] = None
    stickiness: Optional[str] = None
    overrides: List[VariantOverride] = field(default_factory=list)
    cumulative_weight: int = 0


# ---------------------------------------------------------------------------
# Strategy model
# ---------------------------------------------------------------------------


@dataclass
class StrategyDef:
    """A pre-compiled strategy with segments already resolved into constraints.

    ``constraints`` holds the strategy's own constraints **plus** the
    constraints of every resolved segment (flattened at load time). If the
    strategy references a segment id that does not exist, ``has_missing_segment``
    is set ``True`` so the strategy can be forced to evaluate ``False`` (per
    spec ``15-global-constraints``).
    """

    name: str
    parameters: Dict[str, str] = field(default_factory=dict)
    constraints: List[ConstraintDef] = field(default_factory=list)
    segment_ids: List[int] = field(default_factory=list)
    variants: List[VariantDef] = field(default_factory=list)
    sort_order: int = 0
    has_missing_segment: bool = False


# ---------------------------------------------------------------------------
# Dependency model
# ---------------------------------------------------------------------------


@dataclass
class DependencyDef:
    """A dependent-feature relationship (spec ``14-dependent-features``)."""

    feature: str
    enabled: bool = True
    variants: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Feature model
# ---------------------------------------------------------------------------


@dataclass
class FeatureState:
    """A single pre-compiled feature toggle."""

    name: str
    enabled: bool
    project: str = "default"
    type: Optional[str] = None
    stale: bool = False
    impression_data: bool = False
    strategies: List[StrategyDef] = field(default_factory=list)
    variants: List[VariantDef] = field(default_factory=list)
    dependencies: List[DependencyDef] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Top-level engine state
# ---------------------------------------------------------------------------


@dataclass
class FeatureDefinition:
    """Lightweight toggle descriptor returned by ``list_known_toggles``.

    Mirrors the shape consumed by the SDK: objects exposing ``.name``,
    ``.type`` and ``.project`` attributes.
    """

    name: str
    type: Optional[str]
    project: str


@dataclass
class EngineState:
    """The complete pre-compiled state produced by ``parse_client_features``.

    ``features`` is keyed by toggle name for O(1) lookup on the hot path.
    ``raw`` retains the original parsed payload (minus anything we choose to
    normalize) so ``serialize_state`` can round-trip losslessly enough that
    ``take_state(get_state())`` yields identical evaluation results — the
    streaming connector cache relies on this.
    """

    version: int = 2
    features: Dict[str, FeatureState] = field(default_factory=dict)
    segments: Dict[int, Segment] = field(default_factory=dict)
    raw: Optional[dict] = None

    def get_feature(self, name: str) -> Optional[FeatureState]:
        """Return the compiled feature by name, or ``None`` if unknown."""
        return self.features.get(name)

    def list_known_toggles(self) -> List[FeatureDefinition]:
        """Return descriptors for every known toggle (order preserved)."""
        return [
            FeatureDefinition(name=f.name, type=f.type, project=f.project)
            for f in self.features.values()
        ]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_NUM_OPERATORS = frozenset({"NUM_EQ", "NUM_GT", "NUM_GTE", "NUM_LT", "NUM_LTE"})
_SEMVER_OPERATORS = frozenset(
    {"SEMVER_EQ", "SEMVER_GT", "SEMVER_GTE", "SEMVER_LT", "SEMVER_LTE"}
)
_DATE_OPERATORS = frozenset({"DATE_AFTER", "DATE_BEFORE"})
_STR_LIST_OPERATORS = frozenset(
    {"IN", "NOT_IN", "STR_CONTAINS", "STR_STARTS_WITH", "STR_ENDS_WITH"}
)

# RE2 (the engine the spec oracle uses) rejects look-around and backreferences;
# Python's ``re`` accepts them, so we detect and reject them to stay
# spec-compatible (spec 21 ``R90`` / ``R91``).
_RE2_UNSUPPORTED = re.compile(
    r"\(\?="  # lookahead
    r"|\(\?!"  # negative lookahead
    r"|\(\?<="  # lookbehind
    r"|\(\?<!"  # negative lookbehind
    r"|\\[1-9]"  # numeric backreference
)

# POSIX character classes (RE2 supports them; Python ``re`` does not). Mapped to
# ASCII equivalents — these only appear *inside* a ``[...]`` set, so the
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

# Bare inline global-flag group, e.g. ``(?i)`` / ``(?ms)`` (no ``:``). RE2 treats
# these as global regardless of position; Python 3.11+ rejects them unless at the
# very start. We lift any such groups to the front to recover RE2 semantics.
_INLINE_GLOBAL_FLAGS = re.compile(r"\(\?([aiLmsux]+)\)")


def _translate_posix_classes(pattern: str) -> str:
    """Rewrite POSIX ``[[:class:]]`` constructs to Python-compatible classes."""
    if "[:" not in pattern:
        return pattern

    def _sub(m: "re.Match[str]") -> str:
        token = m.group(0)
        return _POSIX_CLASSES.get(token, token)

    return _POSIX_CLASS_RE.sub(_sub, pattern)


def _hoist_inline_flags(pattern: str) -> str:
    """Move bare global inline flags (``(?i)``-style) to the front of ``pattern``."""
    matches = list(_INLINE_GLOBAL_FLAGS.finditer(pattern))
    if not matches:
        return pattern
    flags = ""
    for m in matches:
        for ch in m.group(1):
            if ch not in flags:
                flags += ch
    stripped = _INLINE_GLOBAL_FLAGS.sub("", pattern)
    return f"(?{flags}){stripped}"


def _compile_regex(value: Any, case_insensitive: bool) -> Optional[Pattern]:
    """Compile a REGEX constraint value, returning ``None`` if invalid/RE2-incompatible."""
    if not isinstance(value, str) or _RE2_UNSUPPORTED.search(value):
        return None
    pattern = _hoist_inline_flags(_translate_posix_classes(value))
    flags = re.IGNORECASE if case_insensitive else 0
    try:
        return re.compile(pattern, flags)
    except re.error:
        return None


def _compile_cidrs(values: List[str]) -> List[Any]:
    """Parse IN_CIDR values into ``ip_network`` objects, skipping invalid entries."""
    networks: List[Any] = []
    for raw in values:
        try:
            networks.append(ipaddress.ip_network(raw, strict=False))
        except (ValueError, TypeError):
            continue
    return networks


def _parse_constraint(  # noqa: PLR0912
    raw: dict, warnings: List[str], where: str
) -> ConstraintDef:
    """Build a pre-compiled ``ConstraintDef`` from a raw constraint dict.

    Pre-parses the operator-specific value. An unparseable constraint-side
    value is recorded as a warning and the corresponding ``parsed_*`` stays
    ``None`` (the constraint is still retained; ``constraints.py`` decides the
    evaluation result for an unparseable side).
    """
    context_name = raw["contextName"]
    operator = raw["operator"]
    values = list(raw.get("values") or [])
    value = raw.get("value")
    case_insensitive = bool(raw.get("caseInsensitive", False))
    inverted = bool(raw.get("inverted", False))

    constraint = ConstraintDef(
        context_name=context_name,
        operator=operator,
        values=values,
        value=value,
        case_insensitive=case_insensitive,
        inverted=inverted,
    )

    if operator in _NUM_OPERATORS:
        if value is not None:
            try:
                constraint.parsed_number = float(value)
            except (TypeError, ValueError):
                warnings.append(
                    f"{where}: unparseable numeric constraint value {value!r}"
                )
    elif operator in _SEMVER_OPERATORS:
        if value is not None:
            try:
                constraint.parsed_semver = _SemverVersion.parse(value)
            except (TypeError, ValueError):
                warnings.append(
                    f"{where}: unparseable semver constraint value {value!r}"
                )
    elif operator in _DATE_OPERATORS:
        if value is not None:
            try:
                parsed = _parse_date(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                constraint.parsed_date = parsed
            except (TypeError, ValueError, OverflowError):
                warnings.append(f"{where}: unparseable date constraint value {value!r}")
    elif operator in _STR_LIST_OPERATORS:
        if case_insensitive:
            # Must match the fold the constraints module applies to the context
            # value (``str.upper()``); otherwise pre-folded values never match.
            constraint.parsed_values = [v.upper() for v in values]
    elif operator == "REGEX":
        constraint.parsed_regex = _compile_regex(value, case_insensitive)
        if constraint.parsed_regex is None:
            warnings.append(
                f"{where}: unparseable/RE2-incompatible regex value {value!r}"
            )
    elif operator == "IN_CIDR":
        constraint.parsed_cidrs = _compile_cidrs(values)

    return constraint


def _parse_variant(raw: dict, running_total: int) -> Tuple[VariantDef, int]:
    """Build a ``VariantDef`` and return it plus the updated running weight."""
    weight = int(raw.get("weight", 0))
    running_total += weight
    overrides = [
        VariantOverride(
            context_name=o["contextName"],
            values=list(o.get("values") or []),
        )
        for o in raw.get("overrides") or []
    ]
    variant = VariantDef(
        name=raw["name"],
        weight=weight,
        payload=raw.get("payload"),
        stickiness=raw.get("stickiness"),
        overrides=overrides,
        cumulative_weight=running_total,
    )
    return variant, running_total


def _parse_variants(raw_variants: List[dict]) -> List[VariantDef]:
    variants: List[VariantDef] = []
    running_total = 0
    for raw in raw_variants:
        variant, running_total = _parse_variant(raw, running_total)
        variants.append(variant)
    return variants


def _parse_segment(raw: dict, warnings: List[str]) -> Segment:
    segment_id = int(raw["id"])
    constraints = [
        _parse_constraint(c, warnings, f"segment {segment_id}")
        for c in raw.get("constraints") or []
    ]
    return Segment(segment_id=segment_id, constraints=constraints)


def _parse_strategy(
    raw: dict,
    segments: Dict[int, Segment],
    warnings: List[str],
    feature_name: str,
) -> StrategyDef:
    name = raw["name"]
    parameters = dict(raw.get("parameters") or {})
    segment_ids = [int(s) for s in raw.get("segments") or []]

    own_constraints = [
        _parse_constraint(c, warnings, f"feature {feature_name}")
        for c in raw.get("constraints") or []
    ]

    has_missing_segment = False
    resolved_constraints = list(own_constraints)
    for segment_id in segment_ids:
        segment = segments.get(segment_id)
        if segment is None:
            has_missing_segment = True
            continue
        resolved_constraints.extend(segment.constraints)

    variants = _parse_variants(raw.get("variants") or [])

    return StrategyDef(
        name=name,
        parameters=parameters,
        constraints=resolved_constraints,
        segment_ids=segment_ids,
        variants=variants,
        sort_order=int(raw.get("sortOrder", 0) or 0),
        has_missing_segment=has_missing_segment,
    )


def _parse_dependency(raw: dict) -> DependencyDef:
    return DependencyDef(
        feature=raw["feature"],
        enabled=bool(raw.get("enabled", True)),
        variants=list(raw.get("variants") or []),
    )


def _parse_feature(
    raw: dict,
    segments: Dict[int, Segment],
    warnings: List[str],
) -> FeatureState:
    name = raw["name"]
    strategies = [
        _parse_strategy(s, segments, warnings, name)
        for s in raw.get("strategies") or []
    ]
    variants = _parse_variants(raw.get("variants") or [])
    dependencies = [_parse_dependency(d) for d in raw.get("dependencies") or []]

    return FeatureState(
        name=name,
        enabled=bool(raw.get("enabled", False)),
        project=raw.get("project") or "default",
        type=raw.get("type"),
        stale=bool(raw.get("stale", False)),
        impression_data=bool(raw.get("impressionData", False)),
        strategies=strategies,
        variants=variants,
        dependencies=dependencies,
    )


def _fold_delta_events(  # noqa: PLR0912
    payload: dict,
    prior: Optional[dict],
    warnings: List[str],
) -> dict:
    """Fold an Unleash delta ``{"events": [...]}`` payload into a flat payload.

    The delta/streaming protocol (spec ``19``/``20``) delivers state as an ordered
    list of events instead of a flat ``{features, segments}`` document:

    * ``hydration`` — a full snapshot; resets the accumulated features/segments.
    * ``feature-updated`` — upsert a single feature (keyed by name).
    * ``feature-removed`` — drop a single feature (keyed by ``featureName``).
    * ``segment-updated`` — upsert a single segment (keyed by ``id``).

    Deltas merge onto ``prior`` (the previously applied flat payload) so the
    streaming connector can apply incremental updates across successive
    ``take_state`` calls; a ``hydration`` event discards the prior snapshot.

    Returns a flat ``{"version", "features", "segments"}`` payload ready for the
    normal parsing path.
    """
    features_by_name: Dict[str, dict] = {}
    segments_by_id: Dict[Any, dict] = {}

    if prior:
        for feat in prior.get("features") or []:
            if isinstance(feat, dict) and "name" in feat:
                features_by_name[feat["name"]] = feat
        for seg in prior.get("segments") or []:
            if isinstance(seg, dict) and "id" in seg:
                segments_by_id[seg["id"]] = seg

    version = int((prior or {}).get("version", 2) or 2)

    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            warnings.append("Skipping non-object delta event")
            continue
        etype = event.get("type")
        try:
            if etype == "hydration":
                features_by_name = {f["name"]: f for f in event.get("features") or []}
                segments_by_id = {s["id"]: s for s in event.get("segments") or []}
            elif etype == "feature-updated":
                feature = event["feature"]
                features_by_name[feature["name"]] = feature
            elif etype == "feature-removed":
                features_by_name.pop(event["featureName"], None)
            elif etype == "segment-updated":
                segment = event["segment"]
                segments_by_id[segment["id"]] = segment
            else:
                warnings.append(f"Skipping unknown delta event type {etype!r}")
        except (KeyError, TypeError) as exc:
            warnings.append(f"Skipping malformed delta event ({etype!r}): {exc}")

    return {
        "version": version,
        "features": list(features_by_name.values()),
        "segments": list(segments_by_id.values()),
    }


def parse_client_features(
    json_str: str, prior_raw: Optional[dict] = None
) -> Tuple[EngineState, List[str]]:
    """Parse a raw ``ClientFeatures`` JSON string into a pre-compiled state.

    The payload shape is ``{"version": int, "features": [...], "segments": [...]}``
    or, for the delta/streaming protocol, ``{"events": [...]}`` (which is folded
    into the flat shape via :func:`_fold_delta_events`, merging onto
    ``prior_raw`` when supplied so incremental updates accumulate).
    Parsing is resilient: an individual feature (or constraint/variant) that
    cannot be parsed produces a warning and is skipped or degraded, but never
    aborts the whole payload — matching the ``take_state`` contract.

    :param json_str: Raw ClientFeatures (or delta) JSON.
    :param prior_raw: The previously applied flat payload, used as the base for
        delta merges (ignored for flat / hydration payloads).
    :return: ``(EngineState, warnings)`` where ``warnings`` is a list of
        per-entry human-readable messages (the facade joins these with newlines
        and returns ``None`` when empty).
    """
    warnings: List[str] = []

    try:
        payload = json.loads(json_str)
    except (ValueError, TypeError) as exc:
        warnings.append(f"Failed to parse client features payload: {exc}")
        return EngineState(), warnings

    if not isinstance(payload, dict):
        warnings.append("Client features payload is not a JSON object")
        return EngineState(), warnings

    # Delta/streaming protocol: fold the event list into a flat payload.
    if "events" in payload and "features" not in payload:
        payload = _fold_delta_events(payload, prior_raw, warnings)

    version = int(payload.get("version", 2) or 2)

    # Parse segments first so strategies can resolve them inline.
    segments: Dict[int, Segment] = {}
    for raw_segment in payload.get("segments") or []:
        try:
            segment = _parse_segment(raw_segment, warnings)
            segments[segment.segment_id] = segment
        except (KeyError, TypeError, ValueError) as exc:
            warnings.append(f"Skipping unparseable segment: {exc}")

    features: Dict[str, FeatureState] = {}
    for raw_feature in payload.get("features") or []:
        try:
            feature = _parse_feature(raw_feature, segments, warnings)
        except (KeyError, TypeError, ValueError) as exc:
            warnings.append(f"Skipping unparseable feature: {exc}")
            continue
        features[feature.name] = feature

    state = EngineState(
        version=version,
        features=features,
        segments=segments,
        raw=payload,
    )
    return state, warnings


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _serialize_constraint(constraint: ConstraintDef) -> dict:
    raw: Dict[str, Any] = {
        "contextName": constraint.context_name,
        "operator": constraint.operator,
    }
    if constraint.values:
        raw["values"] = list(constraint.values)
    if constraint.value is not None:
        raw["value"] = constraint.value
    if constraint.case_insensitive:
        raw["caseInsensitive"] = True
    if constraint.inverted:
        raw["inverted"] = True
    return raw


def _serialize_variant(variant: VariantDef) -> dict:
    raw: Dict[str, Any] = {"name": variant.name, "weight": variant.weight}
    if variant.payload is not None:
        raw["payload"] = variant.payload
    if variant.stickiness is not None:
        raw["stickiness"] = variant.stickiness
    if variant.overrides:
        raw["overrides"] = [
            {"contextName": o.context_name, "values": list(o.values)}
            for o in variant.overrides
        ]
    return raw


def _serialize_strategy(strategy: StrategyDef, known_segment_ids: set) -> dict:
    raw: Dict[str, Any] = {"name": strategy.name}
    if strategy.parameters:
        raw["parameters"] = dict(strategy.parameters)
    # Present segments have already been flattened into ``constraints`` at parse
    # time, so we serialize the resolved constraint list inline. We must, however,
    # re-emit any segment ids that did NOT resolve (missing segments) so the
    # re-parsed strategy keeps ``has_missing_segment`` set — otherwise a strategy
    # that should evaluate ``False`` (spec ``15-global-constraints``) would flip
    # to ``True`` on round-trip. Present segment ids are intentionally omitted to
    # avoid double-applying their (already inline) constraints.
    if strategy.constraints:
        raw["constraints"] = [_serialize_constraint(c) for c in strategy.constraints]
    missing_segment_ids = [
        sid for sid in strategy.segment_ids if sid not in known_segment_ids
    ]
    if missing_segment_ids:
        raw["segments"] = missing_segment_ids
    if strategy.variants:
        raw["variants"] = [_serialize_variant(v) for v in strategy.variants]
    if strategy.sort_order:
        raw["sortOrder"] = strategy.sort_order
    return raw


def _serialize_dependency(dependency: DependencyDef) -> dict:
    raw: Dict[str, Any] = {"feature": dependency.feature}
    if dependency.enabled is not True:
        raw["enabled"] = dependency.enabled
    if dependency.variants:
        raw["variants"] = list(dependency.variants)
    return raw


def _serialize_feature(feature: FeatureState, known_segment_ids: set) -> dict:
    raw: Dict[str, Any] = {
        "name": feature.name,
        "enabled": feature.enabled,
        "project": feature.project,
    }
    if feature.type is not None:
        raw["type"] = feature.type
    if feature.stale:
        raw["stale"] = True
    if feature.impression_data:
        raw["impressionData"] = True
    if feature.strategies:
        raw["strategies"] = [
            _serialize_strategy(s, known_segment_ids) for s in feature.strategies
        ]
    if feature.variants:
        raw["variants"] = [_serialize_variant(v) for v in feature.variants]
    if feature.dependencies:
        raw["dependencies"] = [_serialize_dependency(d) for d in feature.dependencies]
    return raw


def serialize_state(state: EngineState) -> str:
    """Serialize an ``EngineState`` back to a ClientFeatures JSON string.

    Must round-trip losslessly enough that ``parse_client_features`` of the
    output reproduces identical evaluation behavior (streaming cache contract).
    Segments are emitted inline as resolved strategy constraints (not as a
    top-level segment table), which is evaluation-equivalent.

    :param state: The engine state to serialize.
    :return: A JSON string consumable by ``parse_client_features`` / ``take_state``.
    """
    known_segment_ids = set(state.segments.keys())
    payload = {
        "version": state.version,
        "features": [
            _serialize_feature(f, known_segment_ids) for f in state.features.values()
        ],
    }
    return json.dumps(payload)
