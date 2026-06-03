#!/usr/bin/env python3
"""Head-to-head throughput benchmark for the Unleash evaluation engine.

Builds one representative ClientFeatures state with several toggle shapes and
times hot loops of ``is_enabled`` (and ``get_variant`` for the variants
scenario) against either the compiled yggdrasil engine or the embedded
pure-Python engine.

Usage::

    python bench_engine.py [--engine yggdrasil|embedded] [--out FILE]

The embedded engine import is wired now but guarded: if it does not yet exist
the script exits with a clear message instead of a traceback.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time

# Number of iterations in each hot loop.
ITERATIONS = 50_000


def load_engine(engine_name: str):
    """Return an instantiated engine for ``engine_name``.

    Both engines expose the same ``UnleashEngine`` API surface
    (``take_state`` / ``is_enabled`` / ``get_variant``). The embedded engine
    may not exist yet, so its import is guarded and fails cleanly.
    """
    if engine_name == "yggdrasil":
        try:
            from yggdrasil_engine.engine import UnleashEngine
        except ImportError as exc:  # pragma: no cover - env guard
            sys.exit(f"yggdrasil engine unavailable: {exc}")
        return UnleashEngine()

    if engine_name == "embedded":
        try:
            from UnleashClient.engine import UnleashEngine  # type: ignore
        except ImportError as exc:
            sys.exit(
                "embedded engine not available yet "
                "(UnleashClient.engine.UnleashEngine): "
                f"{exc}"
            )
        return UnleashEngine()

    sys.exit(f"unknown engine: {engine_name!r}")


def build_state() -> dict:
    """Build ONE ClientFeatures state holding every benchmark scenario."""
    user_ids = [str(n) for n in range(100)]
    return {
        "version": 2,
        "features": [
            # (a) simple always-on toggle.
            {
                "name": "bench.simple",
                "enabled": True,
                "strategies": [{"name": "default"}],
            },
            # (b) toggle with 5 constraints mixing operators.
            {
                "name": "bench.constraints",
                "enabled": True,
                "strategies": [
                    {
                        "name": "default",
                        "constraints": [
                            {
                                "contextName": "strField",
                                "operator": "STR_CONTAINS",
                                "values": ["alpha"],
                                "caseInsensitive": True,
                            },
                            {
                                "contextName": "numField",
                                "operator": "NUM_GTE",
                                "value": "10",
                            },
                            {
                                "contextName": "dateField",
                                "operator": "DATE_AFTER",
                                "value": "2020-01-01T00:00:00.000Z",
                            },
                            {
                                "contextName": "semverField",
                                "operator": "SEMVER_GT",
                                "value": "1.0.0",
                            },
                            {
                                "contextName": "userId",
                                "operator": "IN",
                                "values": user_ids,
                            },
                        ],
                    }
                ],
            },
            # (c) flexibleRollout 50% with userId stickiness.
            {
                "name": "bench.rollout",
                "enabled": True,
                "strategies": [
                    {
                        "name": "flexibleRollout",
                        "parameters": {
                            "rollout": "50",
                            "stickiness": "userId",
                            "groupId": "bench.rollout",
                        },
                    }
                ],
            },
            # (d) toggle with 4 weighted variants + an override.
            {
                "name": "bench.variants",
                "enabled": True,
                "strategies": [{"name": "default"}],
                "variants": [
                    {
                        "name": "v1",
                        "weight": 250,
                        "weightType": "variable",
                        "stickiness": "userId",
                        "payload": {"type": "string", "value": "one"},
                        "overrides": [
                            {"contextName": "userId", "values": ["overridden"]}
                        ],
                    },
                    {
                        "name": "v2",
                        "weight": 250,
                        "weightType": "variable",
                        "stickiness": "userId",
                        "payload": {"type": "string", "value": "two"},
                    },
                    {
                        "name": "v3",
                        "weight": 250,
                        "weightType": "variable",
                        "stickiness": "userId",
                        "payload": {"type": "string", "value": "three"},
                    },
                    {
                        "name": "v4",
                        "weight": 250,
                        "weightType": "variable",
                        "stickiness": "userId",
                        "payload": {"type": "string", "value": "four"},
                    },
                ],
            },
            # (e) userWithId strategy with 100 ids.
            {
                "name": "bench.userwithid",
                "enabled": True,
                "strategies": [
                    {
                        "name": "userWithId",
                        "parameters": {"userIds": ",".join(user_ids)},
                    }
                ],
            },
        ],
    }


def build_context() -> dict:
    """A realistic evaluation context exercising all constraint fields."""
    return {
        "userId": "42",
        "sessionId": "session-abc",
        "remoteAddress": "127.0.0.1",
        "properties": {
            "semverField": "1.2.3",
            "numField": "50",
            "dateField": "2024-06-01T00:00:00.000Z",
            "strField": "this contains alpha here",
        },
    }


def time_loop(fn, iterations: int) -> float:
    """Run ``fn`` ``iterations`` times; return ops/sec."""
    start = time.perf_counter()
    for _ in range(iterations):
        fn()
    elapsed = time.perf_counter() - start
    return iterations / elapsed if elapsed > 0 else float("inf")


def run_benchmark(engine, context: dict) -> dict:
    """Time every scenario; return {scenario_name: ops_per_sec}."""
    scenarios: dict[str, float] = {}

    scenarios["simple"] = time_loop(
        lambda: engine.is_enabled("bench.simple", context), ITERATIONS
    )
    scenarios["constraints"] = time_loop(
        lambda: engine.is_enabled("bench.constraints", context), ITERATIONS
    )
    scenarios["flexible_rollout"] = time_loop(
        lambda: engine.is_enabled("bench.rollout", context), ITERATIONS
    )
    scenarios["variants_is_enabled"] = time_loop(
        lambda: engine.is_enabled("bench.variants", context), ITERATIONS
    )
    scenarios["variants_get_variant"] = time_loop(
        lambda: engine.get_variant("bench.variants", context), ITERATIONS
    )
    scenarios["user_with_id"] = time_loop(
        lambda: engine.is_enabled("bench.userwithid", context), ITERATIONS
    )

    return scenarios


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--engine",
        choices=["yggdrasil", "embedded"],
        default="yggdrasil",
    )
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    engine = load_engine(args.engine)

    state = build_state()
    warnings = engine.take_state(json.dumps(state))
    if warnings:
        sys.exit(f"take_state produced warnings: {warnings}")

    context = build_context()

    # Warm up to stabilise timings (JIT-less but caches/branch prediction).
    for name in (
        "bench.simple",
        "bench.constraints",
        "bench.rollout",
        "bench.variants",
        "bench.userwithid",
    ):
        engine.is_enabled(name, context)
        engine.get_variant(name, context)

    scenarios = run_benchmark(engine, context)

    result = {
        "engine": args.engine,
        "python_version": platform.python_version(),
        "iterations": ITERATIONS,
        "scenarios": scenarios,
    }

    output = json.dumps(result, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(output + "\n")
        print(f"wrote {args.out}")
    print(output)


if __name__ == "__main__":
    main()
