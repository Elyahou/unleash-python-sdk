"""Differential parity check: embedded engine vs yggdrasil on a real client payload.

Feeds the SAME client-features payload to both engines, then compares
``is_enabled`` and ``get_variant`` for every feature across a context corpus
derived from the payload itself (every constraint value, mutations of it,
missing-field cases) plus a randomized stickiness sweep.

Usage:
    python scripts/prod_parity.py /path/to/client_payload.json [--bench]

Requires ``yggdrasil-engine`` (dev dependency) to be installed.
"""

import argparse
import dataclasses
import json
import random
import sys
import time

from yggdrasil_engine.engine import UnleashEngine as YggEngine

from ct_unleash.engine import UnleashEngine as EmbeddedEngine

# Mirrors UnleashClient._safe_context: standard top-level fields, everything
# else stringified under "properties".
BASE_CONTEXT_FIELDS = [
    "userId",
    "sessionId",
    "environment",
    "appName",
    "currentTime",
    "remoteAddress",
    "properties",
]

FIXED_CURRENT_TIME = "2026-06-03T12:00:00+00:00"


def safe_context(raw: dict) -> dict:
    """Shape a raw field dict the way UnleashClient._safe_context would."""
    ctx = {k: str(v) for k, v in raw.items() if k in BASE_CONTEXT_FIELDS}
    ctx["properties"] = {
        str(k): str(v) for k, v in raw.items() if k not in BASE_CONTEXT_FIELDS
    }
    ctx.setdefault("currentTime", FIXED_CURRENT_TIME)
    return ctx


def collect_constraint_pairs(payload: dict):
    """Every (contextName, value) pair used by any strategy or segment."""
    pairs = []
    constraints = []
    for feature in payload.get("features", []):
        for strategy in feature.get("strategies", []):
            constraints.extend(strategy.get("constraints") or [])
    for segment in payload.get("segments", []):
        constraints.extend(segment.get("constraints") or [])
    for constraint in constraints:
        field = constraint["contextName"]
        for value in constraint.get("values") or []:
            pairs.append((field, value))
        if constraint.get("value") is not None:
            pairs.append((field, constraint["value"]))
    return pairs


def build_contexts(payload: dict, sweep_size: int, seed: int = 42):
    rng = random.Random(seed)
    contexts = [safe_context({}), safe_context({"userId": "user-1"})]

    pairs = collect_constraint_pairs(payload)
    seen = set()
    for field, value in pairs:
        for variant_value in (
            value,  # exact match
            value + "-x",  # near miss
            value.upper(),  # case flip (exercises caseInsensitive)
            "",  # empty string
        ):
            key = (field, variant_value)
            if key in seen:
                continue
            seen.add(key)
            contexts.append(
                safe_context({field: variant_value, "userId": f"u-{len(contexts)}"})
            )

    # Randomized stickiness sweep: exercises rollout bucketing for both
    # standard (userId/sessionId) and custom stickiness fields found in
    # the payload's flexibleRollout parameters.
    sticky_fields = {"userId", "sessionId"}
    for feature in payload.get("features", []):
        for strategy in feature.get("strategies", []):
            stickiness = (strategy.get("parameters") or {}).get("stickiness")
            if stickiness and stickiness not in ("default", "random"):
                sticky_fields.add(stickiness)
    for i in range(sweep_size):
        raw = {field: f"{field}-{rng.randrange(10**9)}" for field in sticky_fields}
        contexts.append(safe_context(raw))

    return contexts


def normalize_variant(variant) -> dict:
    """Match UnleashClient._resolve_variant: dataclass -> dict, drop Nones."""
    if variant is None:
        return None
    if dataclasses.is_dataclass(variant):
        variant = dataclasses.asdict(variant)
    return {k: v for k, v in variant.items() if v is not None}


def run_parity(payload: dict, contexts, feature_names) -> int:
    ygg, emb = YggEngine(), EmbeddedEngine()
    state_json = json.dumps(payload)
    ygg_warnings = ygg.take_state(state_json)
    emb_warnings = emb.take_state(state_json)
    if ygg_warnings or emb_warnings:
        print(f"take_state warnings: ygg={ygg_warnings} embedded={emb_warnings}")

    mismatches = []
    comparisons = 0
    for name in feature_names:
        for ctx in contexts:
            comparisons += 1
            expected, actual = ygg.is_enabled(name, ctx), emb.is_enabled(name, ctx)
            if expected != actual:
                mismatches.append(("is_enabled", name, ctx, expected, actual))
            expected_variant = normalize_variant(ygg.get_variant(name, ctx))
            actual_variant = normalize_variant(emb.get_variant(name, ctx))
            if expected_variant != actual_variant:
                mismatches.append(
                    ("get_variant", name, ctx, expected_variant, actual_variant)
                )

    print(
        f"compared {len(feature_names)} features x {len(contexts)} contexts "
        f"({comparisons * 2} calls per engine)"
    )
    if mismatches:
        print(f"FAIL: {len(mismatches)} mismatches")
        for kind, name, ctx, expected, actual in mismatches[:20]:
            print(f"  [{kind}] {name}: ygg={expected} embedded={actual} ctx={ctx}")
        if len(mismatches) > 20:
            print(f"  ... and {len(mismatches) - 20} more")
        return 1
    print("PASS: full parity with yggdrasil on this payload")
    return 0


def run_bench(payload: dict, contexts, feature_names, iterations: int = 50_000):
    print(f"\nbenchmark on production payload ({iterations} calls per engine):")
    rng = random.Random(7)
    sampled = rng.sample(feature_names, min(10, len(feature_names)))
    calls = [(rng.choice(sampled), rng.choice(contexts)) for _ in range(iterations)]
    results = {}
    for label, engine_cls in (("yggdrasil", YggEngine), ("embedded", EmbeddedEngine)):
        engine = engine_cls()
        engine.take_state(json.dumps(payload))
        start = time.perf_counter()
        for name, ctx in calls:
            engine.is_enabled(name, ctx)
        elapsed = time.perf_counter() - start
        results[label] = iterations / elapsed
        print(f"  {label:>10}: {results[label]:>12,.0f} ops/sec")
    print(f"  speedup: {results['embedded'] / results['yggdrasil']:.2f}x")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", help="client-features JSON payload path")
    parser.add_argument("--bench", action="store_true", help="also benchmark")
    parser.add_argument("--sweep", type=int, default=500, help="random sweep size")
    args = parser.parse_args()

    with open(args.payload, encoding="utf-8") as f:
        payload = json.load(f)
    feature_names = [feature["name"] for feature in payload.get("features", [])]
    contexts = build_contexts(payload, sweep_size=args.sweep)

    status = run_parity(payload, contexts, feature_names)
    if args.bench:
        run_bench(payload, contexts, feature_names)
    return status


if __name__ == "__main__":
    sys.exit(main())
