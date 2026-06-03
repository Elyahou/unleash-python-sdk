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

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


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

    A ``parsed_*`` field left as ``None`` after parsing signals that the
    constraint's own value was unparseable; ``constraints.evaluate_constraint``
    defines the resulting truth value (and how ``inverted`` interacts).
    """

    context_name: str
    operator: str
    values: List[str] = field(default_factory=list)
    value: Optional[str] = None
    case_insensitive: bool = False
    inverted: bool = False

    # Pre-compiled, operator-specific representations (populated by parsing).
    parsed_number: Optional[float] = None
    parsed_semver: Optional[Any] = None  # semver.Version (kept Any to avoid import here)
    parsed_date: Optional[datetime] = None
    parsed_values: Optional[List[str]] = None  # case-folded copy of ``values``


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
        raise NotImplementedError


def parse_client_features(json_str: str) -> Tuple[EngineState, List[str]]:
    """Parse a raw ``ClientFeatures`` JSON string into a pre-compiled state.

    The payload shape is ``{"version": int, "features": [...], "segments": [...]}``.
    Parsing is resilient: an individual feature (or constraint/variant) that
    cannot be parsed produces a warning and is skipped or degraded, but never
    aborts the whole payload — matching the ``take_state`` contract.

    :param json_str: Raw ClientFeatures JSON.
    :return: ``(EngineState, warnings)`` where ``warnings`` is a list of
        per-entry human-readable messages (the facade joins these with newlines
        and returns ``None`` when empty).
    """
    raise NotImplementedError


def serialize_state(state: EngineState) -> str:
    """Serialize an ``EngineState`` back to a ClientFeatures JSON string.

    Must round-trip losslessly enough that ``parse_client_features`` of the
    output reproduces identical evaluation behavior (streaming cache contract).

    :param state: The engine state to serialize.
    :return: A JSON string consumable by ``parse_client_features`` / ``take_state``.
    """
    raise NotImplementedError
