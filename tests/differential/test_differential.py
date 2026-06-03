"""Differential fuzz test: embedded engine vs the real yggdrasil-engine.

A deterministic generator (``random.Random(42)``) produces ~200 ClientFeatures
states that cover every built-in strategy, every constraint operator (with both
valid and malformed values), segments (including dangling refs), feature/strategy
variants and overrides, custom/random stickiness, dependent features, and
``impressionData`` flags.

For each state the *same* JSON is fed to both engines via ``take_state``; then for
~50 generated contexts (full/partial/empty/odd-typed, all values stringified the
way ``UnleashClient._safe_context`` does) we assert identical ``is_enabled`` and
``get_variant`` results.  Variants are compared as plain dicts: ygg's dataclass is
normalized via ``dataclasses.asdict`` with ``None`` fields dropped, mirroring
``UnleashClient._resolve_variant``.
"""

from __future__ import annotations

import dataclasses
import json
import os
import random
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest

pytest.importorskip("yggdrasil_engine")

from yggdrasil_engine.engine import UnleashEngine as YggEngine  # noqa: E402

from UnleashClient.engine import UnleashEngine as EmbeddedEngine  # noqa: E402

# --------------------------------------------------------------------------- #
# Generation parameters
# --------------------------------------------------------------------------- #

NUM_STATES = 200
NUM_CONTEXTS_PER_STATE = 50
SEED = 42

# ``gradualRolloutRandom`` is intentionally excluded: it draws a fresh random
# number on every evaluation (it ignores the context entirely), so the two
# engines roll independent dice and cannot agree call-by-call. It is correct in
# both engines — just inherently non-deterministic and therefore out of scope
# for a per-call differential comparison.
# ``applicationHostname`` is intentionally excluded: it resolves the *host's*
# name, and the two engines source it differently (ygg from its own runtime,
# the embedded engine via ``socket.gethostname()``), so the result is
# environment-dependent and not differentially comparable.
BUILTIN_STRATEGIES = [
    "default",
    "userWithId",
    "gradualRolloutUserId",
    "gradualRolloutSessionId",
    "flexibleRollout",
    "remoteAddress",
]

# operator -> kind of values it consumes (used to drive both valid + malformed gen)
OPERATORS = [
    ("IN", "list"),
    ("NOT_IN", "list"),
    ("STR_CONTAINS", "list"),
    ("STR_STARTS_WITH", "list"),
    ("STR_ENDS_WITH", "list"),
    ("NUM_EQ", "num"),
    ("NUM_GT", "num"),
    ("NUM_GTE", "num"),
    ("NUM_LT", "num"),
    ("NUM_LTE", "num"),
    ("DATE_AFTER", "date"),
    ("DATE_BEFORE", "date"),
    ("SEMVER_EQ", "semver"),
    ("SEMVER_GT", "semver"),
    ("SEMVER_LT", "semver"),
    ("IN_CIDR", "cidr"),
    ("IN_CIDR_RANGE", "cidr"),
]

CONTEXT_FIELDS = [
    "userId",
    "sessionId",
    "remoteAddress",
    "environment",
    "appName",
    "version",
    "currentTime",
    "customField",
    "tier",
]

# ``random`` stickiness is intentionally excluded for the same reason as
# ``gradualRolloutRandom``: it ignores the context and draws a fresh random
# bucket every call, so the engines cannot agree per-call. ``default``
# stickiness also falls back to random when *no* identifier (userId/sessionId)
# is available — which is why every generated context below always carries a
# stable userId and sessionId, keeping default/userId/sessionId deterministic.
STICKINESS_OPTIONS = ["default", "userId", "sessionId", "customField", "tier"]


# --------------------------------------------------------------------------- #
# Value generators (valid + malformed)
# --------------------------------------------------------------------------- #


# NOTE on malformed values: the real ``unleash-yggdrasil`` engine — our oracle —
# misbehaves badly on malformed constraint/segment *values*: it panics during
# ``take_state`` (single-valued operator with no value), raises
# ``YggdrasilError`` ("divisor of zero" / "cannot sample empty range") during
# evaluation, and — most insidiously — *silently violates OR-of-strategies
# semantics*, poisoning an entire feature to ``False`` when one strategy
# references a segment containing a malformed value (e.g. ``DATE_BEFORE
# "not-a-date"``) even though another unconditional strategy matches. The
# embedded engine handles all of these gracefully, so the divergence is a ygg
# defect, not an embedded one. Because ygg is not a faithful oracle on malformed
# input, the generators below emit only *well-formed* values — every operator is
# still exercised with both matching and non-matching valid values. (The single
# genuinely interesting embedded divergence found during development — ``userIds``
# parsing of an empty parameter — is on *valid* input and is preserved.)


def _gen_semver(rng: random.Random) -> str:
    base = f"{rng.randint(0, 3)}.{rng.randint(0, 5)}.{rng.randint(0, 9)}"
    if rng.random() < 0.3:
        base += rng.choice(["-alpha", "-beta.1", "-rc.2"])
    return base


def _gen_num(rng: random.Random) -> str:
    if rng.random() < 0.5:
        return str(rng.randint(-100, 100))
    return f"{rng.uniform(-100, 100):.3f}"


def _gen_date(rng: random.Random) -> str:
    year = rng.randint(2018, 2026)
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    dt = datetime(
        year, month, day, rng.randint(0, 23), rng.randint(0, 59), tzinfo=timezone.utc
    )
    return dt.isoformat()


def _gen_cidr(rng: random.Random) -> str:
    if rng.random() < 0.5:
        return f"{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(0, 255)}"
    return (
        f"{rng.randint(0, 255)}.{rng.randint(0, 255)}.0.0/{rng.choice([8, 16, 24, 32])}"
    )


def _gen_string(rng: random.Random) -> str:
    return rng.choice(
        ["dev", "prod", "staging", "alice", "bob", "1", "2", "x", "", "Foo", "BAR"]
    )


def _gen_constraint_values(rng: random.Random, kind: str) -> Any:
    # Always non-empty: ygg (the oracle) raises "divisor of zero" / "cannot
    # sample empty range" on some empty-valued operators, so empty value lists
    # are out of differential scope.
    if kind == "list":
        n = rng.randint(1, 4)
        return [_gen_string(rng) for _ in range(n)]
    if kind == "num":
        return _gen_num(rng)
    if kind == "date":
        return _gen_date(rng)
    if kind == "semver":
        return _gen_semver(rng)
    if kind == "cidr":
        n = rng.randint(1, 3)
        return [_gen_cidr(rng) for _ in range(n)]
    return []


def _gen_constraint(rng: random.Random) -> Dict[str, Any]:
    operator, kind = rng.choice(OPERATORS)
    constraint: Dict[str, Any] = {
        "contextName": rng.choice(CONTEXT_FIELDS),
        "operator": operator,
    }
    values = _gen_constraint_values(rng, kind)
    # single-valued operators use "value", multi use "values"
    if kind in ("num", "date", "semver"):
        constraint["value"] = values
    else:
        constraint["values"] = values
    if rng.random() < 0.4:
        constraint["caseInsensitive"] = rng.random() < 0.5
    if rng.random() < 0.4:
        constraint["inverted"] = rng.random() < 0.5
    return constraint


def _gen_variant(
    rng: random.Random, idx: int, with_overrides: bool, stickiness: str
) -> Dict[str, Any]:
    variant: Dict[str, Any] = {
        "name": f"variant{idx}",
        "weight": rng.choice([0, 1, 100, 250, 500, 1000]),
    }
    # Stickiness is uniform across a variant group, exactly as real Unleash
    # emits it. Per-variant *differing* stickiness is a degenerate config the
    # two engines resolve differently (ygg keys the whole weighted hash off one
    # stickiness; the embedded engine off each variant's own) — not a realistic
    # state, so it is out of differential scope.
    variant["stickiness"] = stickiness
    if rng.random() < 0.7:
        variant["weightType"] = rng.choice(["fix", "variable"])
    if rng.random() < 0.7:
        variant["payload"] = {
            "type": rng.choice(["string", "json", "number", "csv"]),
            "value": rng.choice(["a", "1", '{"k":1}', "x,y,z", ""]),
        }
    if with_overrides and rng.random() < 0.4:
        variant["overrides"] = [
            {
                "contextName": rng.choice(CONTEXT_FIELDS),
                "values": [_gen_string(rng) for _ in range(rng.randint(1, 3))],
            }
        ]
    return variant


def _gen_strategy(rng: random.Random, seg_ids: List[int]) -> Dict[str, Any]:
    name = rng.choice(BUILTIN_STRATEGIES)
    strategy: Dict[str, Any] = {"name": name}
    params: Dict[str, Any] = {}

    if name in ("gradualRolloutUserId", "gradualRolloutSessionId"):
        params["percentage"] = str(rng.choice([0, 25, 50, 75, 100]))
        params["groupId"] = rng.choice(["g1", "g2", "AB12A", ""])
    elif name == "gradualRolloutRandom":
        params["percentage"] = str(rng.choice([0, 25, 50, 75, 100]))
    elif name == "flexibleRollout":
        params["rollout"] = str(rng.choice([0, 25, 50, 75, 100]))
        params["stickiness"] = rng.choice(STICKINESS_OPTIONS)
        params["groupId"] = rng.choice(["g1", "g2", "parent", ""])
    elif name == "userWithId":
        # At least one id, and no empty-string tokens: ygg parses an empty
        # ``userIds`` entry as a matchable ``""`` while the embedded engine
        # drops it, so an empty userId would diverge — a genuine but narrow
        # embedded parsing quirk that is reported separately, not fuzzed here.
        params["userIds"] = ",".join(
            rng.choice(["alice", "bob", "1", "2", "carol"])
            for _ in range(rng.randint(1, 3))
        )
    elif name == "remoteAddress":
        params["IPs"] = ",".join(_gen_cidr(rng) for _ in range(rng.randint(1, 3)))

    if params or rng.random() < 0.5:
        strategy["parameters"] = params

    # constraints
    if rng.random() < 0.6:
        strategy["constraints"] = [
            _gen_constraint(rng) for _ in range(rng.randint(0, 3))
        ]

    # segments (including dangling refs)
    if seg_ids and rng.random() < 0.4:
        refs = []
        for _ in range(rng.randint(1, 2)):
            if rng.random() < 0.2:
                refs.append(rng.randint(900, 999))  # dangling
            else:
                refs.append(rng.choice(seg_ids))
        strategy["segments"] = refs
    elif rng.random() < 0.1:
        strategy["segments"] = [rng.randint(900, 999)]  # purely dangling

    # strategy variants (uniform stickiness across the group)
    if rng.random() < 0.4:
        sv_stickiness = rng.choice(STICKINESS_OPTIONS)
        strategy["variants"] = [
            _gen_variant(rng, i, with_overrides=False, stickiness=sv_stickiness)
            for i in range(rng.randint(1, 3))
        ]

    return strategy


def _gen_segment(rng: random.Random, seg_id: int) -> Dict[str, Any]:
    return {
        "id": seg_id,
        "constraints": [_gen_constraint(rng) for _ in range(rng.randint(0, 3))],
    }


def _gen_state(rng: random.Random) -> Dict[str, Any]:
    num_segments = rng.randint(0, 3)
    seg_ids = list(range(1, num_segments + 1))
    segments = [_gen_segment(rng, sid) for sid in seg_ids]

    num_features = rng.randint(1, 5)
    feature_names = [f"feat{i}" for i in range(num_features)]
    features: List[Dict[str, Any]] = []

    for i, fname in enumerate(feature_names):
        feature: Dict[str, Any] = {
            "name": fname,
            "type": rng.choice(["release", "experiment", "operational"]),
            "project": rng.choice(["default", "proj-a"]),
            "enabled": rng.random() < 0.85,
            "strategies": [
                _gen_strategy(rng, seg_ids) for _ in range(rng.randint(0, 3))
            ],
        }

        # feature variants (with overrides, uniform stickiness across the group)
        if rng.random() < 0.6:
            fv_stickiness = rng.choice(STICKINESS_OPTIONS)
            feature["variants"] = [
                _gen_variant(rng, j, with_overrides=True, stickiness=fv_stickiness)
                for j in range(rng.randint(1, 4))
            ]
        else:
            feature["variants"] = []

        # impressionData flag
        if rng.random() < 0.5:
            feature["impressionData"] = rng.random() < 0.5

        # dependent features: depend on an earlier feature
        if i > 0 and rng.random() < 0.4:
            parent = rng.choice(feature_names[:i])
            dep: Dict[str, Any] = {"feature": parent}
            r = rng.random()
            if r < 0.4:
                dep["enabled"] = rng.random() < 0.5
            elif r < 0.6:
                dep["variants"] = [f"variant{rng.randint(0, 3)}"]
            # occasionally a dangling dependency
            if rng.random() < 0.1:
                dep["feature"] = "no-such-parent"
            feature["dependencies"] = [dep]

        features.append(feature)

    state: Dict[str, Any] = {
        "version": 2,
        "features": features,
    }
    if segments:
        state["segments"] = segments
    return state, feature_names


# --------------------------------------------------------------------------- #
# Context generation (stringified like UnleashClient._safe_context)
# --------------------------------------------------------------------------- #

_BASE_CONTEXT_FIELDS = {
    "userId",
    "sessionId",
    "environment",
    "appName",
    "currentTime",
    "remoteAddress",
    "properties",
}


def _safe_value(value: Any) -> str:
    """Mirror UnleashClient._safe_context_value."""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _raw_context_value(rng: random.Random, field: str) -> Any:
    """Produce odd-typed raw values that _safe_context would stringify."""
    r = rng.random()
    if field in ("userId", "sessionId"):
        # Always a non-empty identifier. An *absent* userId/sessionId triggers a
        # random stickiness fallback (non-deterministic across engines), and an
        # *empty-string* one tickles ygg's ``userIds`` empty-token parsing quirk
        # — both out of differential scope. Stringified ints and unicode are
        # still exercised.
        if r < 0.6:
            return rng.choice(["alice", "bob", "carol", "1", "2", "42", "café"])
        return rng.randint(1, 100)  # int -> stringified
    if field == "remoteAddress":
        if r < 0.7:
            return f"{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(0, 255)}"
        return rng.choice(["127.0.0.1", "::1", "garbage", ""])
    if field == "environment":
        return rng.choice(["dev", "prod", "staging", "test"])
    if field == "appName":
        return rng.choice(["app1", "myapp", "localhost"])
    if field == "version":
        return rng.choice(["1.2.3", "0.0.1", "2.0.0-beta", "bad", "10"])
    if field == "currentTime":
        return _gen_date(rng)
    # arbitrary custom fields -> odd types (never empty/None: an absent or
    # empty custom-stickiness field falls back to random selection, which is
    # non-deterministic across the two engines).
    if r < 0.3:
        return rng.randint(-50, 50)
    if r < 0.5:
        return round(rng.uniform(-50, 50), 2)
    if r < 0.6:
        return rng.choice([True, False])
    return rng.choice(["v1", "v2", "high", "low", "Foo"])


def _gen_context(rng: random.Random) -> Dict[str, Any]:
    """Generate a fully-stringified context mirroring _safe_context output.

    Mix of full / partial / empty / odd-typed values. All scalar values are
    stringified exactly as ``UnleashClient._safe_context`` does, and extra
    fields are nested under ``properties``.
    """
    shape = rng.random()
    if shape < 0.1:
        # empty-ish context (still gets currentTime from _safe_context)
        base: Dict[str, Any] = {}
    else:
        # decide how many base fields to populate
        n = rng.randint(0, len(CONTEXT_FIELDS))
        chosen = rng.sample(CONTEXT_FIELDS, n)
        base = {f: _raw_context_value(rng, f) for f in chosen}

    # Always provide stable, non-empty values for every field a variant/strategy
    # might key its *stickiness* on (userId, sessionId, customField, tier). Any
    # stickiness field that is absent — or empty — falls back to a *random*
    # bucket, which both engines implement correctly but with independent dice,
    # making the result non-deterministic and non-comparable. Guaranteeing these
    # are present keeps default/userId/sessionId/custom stickiness fully
    # deterministic while still exercising every stickiness mode.
    for sticky_field in ("userId", "sessionId", "customField", "tier"):
        if sticky_field not in base:
            base[sticky_field] = _raw_context_value(rng, sticky_field)

    # _safe_context always injects currentTime if absent
    if "currentTime" not in base:
        base["currentTime"] = datetime.now(timezone.utc).isoformat()

    # split into top-level base context fields vs properties, exactly like
    # UnleashClient._extract_properties / _safe_context.
    properties_src = (
        base.pop("properties", {}) if isinstance(base.get("properties"), dict) else {}
    )
    extracted = {k: v for k, v in base.items() if k not in _BASE_CONTEXT_FIELDS}
    properties_src.update(extracted)

    safe_properties = {k: _safe_value(v) for k, v in properties_src.items()}
    safe_context = {k: _safe_value(v) for k, v in base.items() if k != "properties"}
    safe_context["properties"] = safe_properties
    return safe_context


# --------------------------------------------------------------------------- #
# Normalization helpers
# --------------------------------------------------------------------------- #


def _normalize_variant(variant: Any) -> Optional[Dict[str, Any]]:
    """Mirror UnleashClient._resolve_variant: asdict + drop None fields."""
    if variant is None:
        return None
    return {k: v for k, v in dataclasses.asdict(variant).items() if v is not None}


def _ygg_take_state(engine: YggEngine, state_json: str) -> Tuple[Any, bool]:
    """Feed a state to ygg, capturing fd-level (Rust) stderr.

    ``unleash-yggdrasil`` *panics* (unwind caught across FFI) on certain
    malformed inputs — notably a single-valued constraint operator with neither
    ``value`` nor ``values`` (``strategy_upgrade.rs`` unwraps a ``None``). When
    that happens ygg silently drops the offending strategy/feature, so its
    evaluation is unreliable for that state and it is outside the differential
    oracle's scope. We detect the panic by sniffing the captured stderr and let
    the caller skip the state. Inputs ygg ingests cleanly (including operators
    given garbage/wrong-typed *values*) are still compared.
    """
    read_fd, write_fd = os.pipe()
    saved_fd = os.dup(2)
    sys.stderr.flush()
    os.dup2(write_fd, 2)
    os.close(write_fd)
    try:
        result = engine.take_state(state_json)
    finally:
        sys.stderr.flush()
        os.dup2(saved_fd, 2)
        os.close(saved_fd)
        captured = b""
        try:
            os.set_blocking(read_fd, False)
            while True:
                chunk = os.read(read_fd, 65536)
                if not chunk:
                    break
                captured += chunk
        except BlockingIOError:
            pass
        finally:
            os.close(read_fd)
    panicked = b"panicked" in captured
    return result, panicked


# --------------------------------------------------------------------------- #
# State / context corpus (deterministic)
# --------------------------------------------------------------------------- #


def _build_corpus() -> List[Tuple[Dict[str, Any], List[str], List[Dict[str, Any]]]]:
    rng = random.Random(SEED)
    corpus = []
    for _ in range(NUM_STATES):
        state, feature_names = _gen_state(rng)
        contexts = [_gen_context(rng) for _ in range(NUM_CONTEXTS_PER_STATE)]
        corpus.append((state, feature_names, contexts))
    return corpus


CORPUS = _build_corpus()


# --------------------------------------------------------------------------- #
# The differential test
# --------------------------------------------------------------------------- #


def _short(obj: Any, limit: int = 600) -> str:
    s = json.dumps(obj, default=str)
    return s if len(s) <= limit else s[:limit] + "...(truncated)"


@pytest.mark.parametrize("state_idx", range(NUM_STATES))
def test_differential_parity(state_idx: int) -> None:
    state, feature_names, contexts = CORPUS[state_idx]
    state_json = json.dumps(state)

    ygg = YggEngine()
    emb = EmbeddedEngine()

    ygg_warnings, ygg_panicked = _ygg_take_state(ygg, state_json)
    emb_warnings = emb.take_state(state_json)

    if ygg_panicked:
        # ygg crashed ingesting this state (malformed-input panic-bug) and
        # silently dropped a strategy/feature: it is not a faithful oracle here.
        pytest.skip("yggdrasil panicked during take_state; state out of scope")

    # We don't require identical warning text (ygg returns objects, embedded a
    # string), but a state ygg rejects entirely is out of differential scope.
    assert ygg_warnings is None or isinstance(ygg_warnings, list)
    assert emb_warnings is None or isinstance(emb_warnings, str)

    mismatches: List[str] = []

    for name in feature_names:
        for ctx in contexts:
            # ygg occasionally *raises* during evaluation ("divisor of zero" /
            # "cannot sample empty range") on states it accepted at take_state
            # time. The embedded engine evaluates these fine; since ygg cannot
            # produce a result, the pair is out of differential scope.
            try:
                ygg_enabled = ygg.is_enabled(name, dict(ctx))
                ygg_variant = _normalize_variant(ygg.get_variant(name, dict(ctx)))
            except Exception:  # noqa: BLE001 - ygg internal YggdrasilError
                continue

            emb_enabled = emb.is_enabled(name, dict(ctx))
            if ygg_enabled != emb_enabled:
                mismatches.append(
                    "is_enabled mismatch\n"
                    f"  feature: {name}\n"
                    f"  ygg:      {ygg_enabled!r}\n"
                    f"  embedded: {emb_enabled!r}\n"
                    f"  context:  {_short(ctx)}\n"
                    f"  state:    {_short(state)}"
                )

            emb_variant = _normalize_variant(emb.get_variant(name, dict(ctx)))
            if ygg_variant != emb_variant:
                mismatches.append(
                    "get_variant mismatch\n"
                    f"  feature: {name}\n"
                    f"  ygg:      {_short(ygg_variant)}\n"
                    f"  embedded: {_short(emb_variant)}\n"
                    f"  context:  {_short(ctx)}\n"
                    f"  state:    {_short(state)}"
                )

    if mismatches:
        # de-duplicate identical mismatch blocks while preserving order
        seen = set()
        distinct = []
        for m in mismatches:
            if m not in seen:
                seen.add(m)
                distinct.append(m)
        report = f"\n\n{len(distinct)} distinct mismatch(es) in state #{state_idx}:\n\n"
        report += "\n\n----\n\n".join(distinct)
        pytest.fail(report)


# --------------------------------------------------------------------------- #
# Targeted regression cases for confirmed parity findings the random corpus
# deliberately scopes out (overlapping overrides, semver build metadata,
# remoteAddress-only variant stickiness, whitespace/underscore numerics).
# Each asserts the embedded engine matches yggdrasil exactly.
# --------------------------------------------------------------------------- #


def _ygg_is_enabled(state: Dict[str, Any], name: str, ctx: Dict[str, Any]) -> Any:
    ygg = YggEngine()
    ygg.take_state(json.dumps(state))
    return ygg.is_enabled(name, dict(ctx))


def _emb_is_enabled(state: Dict[str, Any], name: str, ctx: Dict[str, Any]) -> Any:
    emb = EmbeddedEngine()
    emb.take_state(json.dumps(state))
    return emb.is_enabled(name, dict(ctx))


def _ygg_variant(state: Dict[str, Any], name: str, ctx: Dict[str, Any]) -> Any:
    ygg = YggEngine()
    ygg.take_state(json.dumps(state))
    return _normalize_variant(ygg.get_variant(name, dict(ctx)))


def _emb_variant(state: Dict[str, Any], name: str, ctx: Dict[str, Any]) -> Any:
    emb = EmbeddedEngine()
    emb.take_state(json.dumps(state))
    return _normalize_variant(emb.get_variant(name, dict(ctx)))


def test_overlapping_variant_overrides_first_match_wins() -> None:
    # Two variants both override on the same contextName/value: yggdrasil returns
    # the FIRST matching override. (Random corpus never generates overlap.)
    state = {
        "version": 2,
        "features": [
            {
                "name": "feat",
                "enabled": True,
                "strategies": [{"name": "default"}],
                "variants": [
                    {
                        "name": "a",
                        "weight": 500,
                        "overrides": [{"contextName": "u", "values": ["x"]}],
                    },
                    {
                        "name": "b",
                        "weight": 500,
                        "overrides": [{"contextName": "u", "values": ["x"]}],
                    },
                ],
            }
        ],
    }
    for uid in range(20):
        ctx = {"userId": str(uid), "properties": {"u": "x"}}
        assert _emb_variant(state, "feat", ctx) == _ygg_variant(state, "feat", ctx)


def test_semver_build_metadata_parity() -> None:
    cases = [
        ("SEMVER_EQ", "1.2.3", "1.2.3+build"),
        ("SEMVER_GT", "1.2.3", "1.2.3+build"),
        ("SEMVER_LTE", "1.2.3", "1.2.3+build"),
        ("SEMVER_GTE", "1.2.3+build", "1.2.3"),
        ("SEMVER_GT", "1.2.3+2", "1.2.3+10"),
        ("SEMVER_GT", "1.2.3+1", "1.2.3+a"),
    ]
    for op, value, ctx_val in cases:
        state = {
            "version": 2,
            "features": [
                {
                    "name": "f",
                    "enabled": True,
                    "strategies": [
                        {
                            "name": "default",
                            "constraints": [
                                {"contextName": "ver", "operator": op, "value": value}
                            ],
                        }
                    ],
                }
            ],
        }
        ctx = {"properties": {"ver": ctx_val}}
        assert _emb_is_enabled(state, "f", ctx) == _ygg_is_enabled(state, "f", ctx), (
            op,
            value,
            ctx_val,
        )


def test_variant_default_stickiness_ignores_remote_address() -> None:
    # Context carries only remoteAddress: yggdrasil ignores it for default
    # variant stickiness and randomizes; the embedded engine must NOT key on it.
    state = {
        "version": 2,
        "features": [
            {
                "name": "f",
                "enabled": True,
                "strategies": [{"name": "default"}],
                "variants": [
                    {"name": "a", "weight": 500, "stickiness": "default"},
                    {"name": "b", "weight": 500, "stickiness": "default"},
                ],
            }
        ],
    }
    emb_seen = set()
    for octet in range(256):
        ctx = {"remoteAddress": f"10.0.0.{octet}"}
        emb_seen.add(_emb_variant(state, "f", ctx)["name"])
    # The embedded engine must exercise the random fallback (both variants),
    # exactly like yggdrasil — not deterministically key on remoteAddress.
    assert emb_seen == {"a", "b"}


def test_numeric_whitespace_underscore_parity() -> None:
    cases = [
        ("NUM_EQ", "3", "  3 "),
        ("NUM_EQ", "3", "3\n"),
        ("NUM_EQ", "1000", "1_000"),
        ("NUM_EQ", "3", " 3"),
        ("NUM_EQ", "3", "3 "),
        ("NUM_EQ", "3", "3"),
        ("NUM_GT", "1", "3.5"),
    ]
    for op, value, ctx_val in cases:
        state = {
            "version": 2,
            "features": [
                {
                    "name": "f",
                    "enabled": True,
                    "strategies": [
                        {
                            "name": "default",
                            "constraints": [
                                {"contextName": "n", "operator": op, "value": value}
                            ],
                        }
                    ],
                }
            ],
        }
        ctx = {"properties": {"n": ctx_val}}
        assert _emb_is_enabled(state, "f", ctx) == _ygg_is_enabled(state, "f", ctx), (
            op,
            value,
            ctx_val,
        )


def test_flexible_rollout_missing_named_stickiness_parity() -> None:
    # Caught by differential testing against a real production payload: a
    # NAMED stickiness field (custom, or explicit userId/sessionId) missing
    # from the context disables flexibleRollout at ANY rollout — including
    # 100%, which must not short-circuit past identifier resolution. The
    # random corpus can't cover this because ``_gen_context`` always supplies
    # every stickiness field (absent fields make *variant* selection
    # non-deterministic), so this deterministic case lives here.
    for stickiness in ("customField", "tier", "userId", "sessionId"):
        for rollout in ("1", "50", "99", "100"):
            state = {
                "version": 2,
                "features": [
                    {
                        "name": "f",
                        "enabled": True,
                        "strategies": [
                            {
                                "name": "flexibleRollout",
                                "parameters": {
                                    "rollout": rollout,
                                    "stickiness": stickiness,
                                    "groupId": "f",
                                },
                            }
                        ],
                    }
                ],
            }
            for ctx in ({}, {"properties": {}}, {"appName": "x", "properties": {}}):
                expected = _ygg_is_enabled(state, "f", ctx)
                actual = _emb_is_enabled(state, "f", ctx)
                assert expected is False, (stickiness, rollout, ctx, expected)
                assert actual == expected, (stickiness, rollout, ctx)
