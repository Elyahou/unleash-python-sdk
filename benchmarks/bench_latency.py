#!/usr/bin/env python3
"""Per-call latency benchmark: yggdrasil vs embedded engine.

Times every individual ``is_enabled`` and ``get_variant`` call with
``time.perf_counter_ns`` across multiple runs against the synthetic state
from ``bench_engine.py``, then reports mean / p50 / p99 per engine.

Usage::

    python bench_latency.py [--runs 5] [--calls 50000]

Note: the ~50-80ns timer overhead is included identically for both engines,
so relative comparisons are fair; absolute numbers are slightly inflated.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import time

from bench_engine import build_context, build_state, load_engine

# Cycle across every toggle shape so the distribution covers simple,
# constrained, rollout, variant, and userWithId evaluation paths.
TOGGLES = [
    "bench.simple",
    "bench.constraints",
    "bench.rollout",
    "bench.variants",
    "bench.userwithid",
]

WARMUP_CALLS = 2_000


def percentile(sorted_ns: list, pct: float) -> float:
    idx = min(len(sorted_ns) - 1, max(0, round(pct / 100 * (len(sorted_ns) - 1))))
    return sorted_ns[idx]


def time_calls(method, calls: list) -> list:
    """Time each call individually; returns per-call nanoseconds."""
    timer = time.perf_counter_ns
    samples = []
    append = samples.append
    for name, ctx in calls:
        start = timer()
        method(name, ctx)
        append(timer() - start)
    return samples


def bench_engine_latency(engine_name: str, runs: int, calls_per_run: int) -> dict:
    state_json = json.dumps(build_state())
    context = build_context()
    plan = [(TOGGLES[i % len(TOGGLES)], context) for i in range(calls_per_run)]

    results = {}
    for method_name in ("is_enabled", "get_variant"):
        merged = []
        run_means = []
        for _ in range(runs):
            engine = load_engine(engine_name)
            engine.take_state(state_json)
            method = getattr(engine, method_name)
            time_calls(method, plan[:WARMUP_CALLS])  # warmup, discarded
            samples = time_calls(method, plan)
            merged.extend(samples)
            run_means.append(statistics.fmean(samples))
        merged.sort()
        results[method_name] = {
            "mean_us": statistics.fmean(merged) / 1000,
            "p50_us": percentile(merged, 50) / 1000,
            "p99_us": percentile(merged, 99) / 1000,
            "run_mean_spread_us": (max(run_means) - min(run_means)) / 1000,
            "samples": len(merged),
        }
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--calls", type=int, default=50_000)
    args = parser.parse_args()

    print(
        f"python {platform.python_version()} | {args.runs} runs x "
        f"{args.calls} calls per method per engine | toggles: {len(TOGGLES)} shapes"
    )
    all_results = {}
    for engine_name in ("yggdrasil", "embedded"):
        all_results[engine_name] = bench_engine_latency(
            engine_name, args.runs, args.calls
        )

    header = (
        f"{'method':<14}{'engine':<12}{'mean':>10}{'p50':>10}{'p99':>10}{'spread':>10}"
    )
    print()
    print(header)
    print("-" * len(header))
    for method_name in ("is_enabled", "get_variant"):
        for engine_name in ("yggdrasil", "embedded"):
            r = all_results[engine_name][method_name]
            print(
                f"{method_name:<14}{engine_name:<12}"
                f"{r['mean_us']:>8.2f}us{r['p50_us']:>8.2f}us"
                f"{r['p99_us']:>8.2f}us{r['run_mean_spread_us']:>8.2f}us"
            )
        ygg = all_results["yggdrasil"][method_name]
        emb = all_results["embedded"][method_name]
        print(
            f"{'':<14}{'speedup':<12}"
            f"{ygg['mean_us'] / emb['mean_us']:>9.2f}x"
            f"{ygg['p50_us'] / emb['p50_us']:>9.2f}x"
            f"{ygg['p99_us'] / emb['p99_us']:>9.2f}x"
        )
        print()


if __name__ == "__main__":
    main()
