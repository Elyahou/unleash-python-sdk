# Embedded Evaluation Engine — Design Spec

**Date:** 2026-06-03
**Status:** Approved
**Branch:** `embedded-engine`

## Goal

Fork the Unleash Python SDK so it no longer depends on the compiled Rust
`yggdrasil-engine` package. Replace it with a pure-Python evaluation engine
embedded in the SDK (`UnleashClient/engine/`) that is a drop-in replacement
for the `UnleashEngine` API surface the SDK already uses.

## Motivation

**Performance.** Every `is_enabled` / `get_variant` call today serializes the
context to JSON, crosses the Python→Rust FFI boundary, and deserializes the
result. For high-call-rate workloads this marshalling dominates evaluation
cost. A native Python engine evaluates on plain dicts in-process, eliminating
that overhead entirely.

Secondary benefits: pure-Python wheel (no native binary), full control over
evaluation logic.

## Acceptance criteria (hard gates)

1. **Exact parity:** the official Unleash client-specification suite
   (v6.1.0, already wired in `tests/specification_tests/`) passes 100%.
2. **Differential parity:** a randomized differential test — same states and
   contexts fed to both the embedded engine and the real `yggdrasil-engine`
   (kept as a dev-only dependency) — produces identical `is_enabled` and
   `get_variant` results.
3. **Faster than Yggdrasil:** a pytest-benchmark head-to-head of hot-loop
   `is_enabled` / `get_variant` throughput must show the embedded engine
   beating the FFI engine.
4. All existing unit and integration tests pass (except those covering
   explicitly dropped capabilities, which are removed/updated).

## Scope

### In scope (engine API surface)

| Method | Notes |
|---|---|
| `take_state(json) -> warnings` | Parse ClientFeatures JSON; per-feature warnings for unparseable entries |
| `get_state() -> json` | Serialize current state (used by streaming connector cache) |
| `is_enabled(name, context) -> bool \| None` | `None` when toggle unknown (triggers SDK fallback) |
| `get_variant(name, context) -> variant \| None` | Same dict-compatible shape as ygg's (consumed via `asdict`) |
| `list_known_toggles()` | Toggle name/type/project list |
| `count_toggle`, `count_variant`, `get_metrics` | yes/no + variant counters; bucket with start/stop timestamps |
| `should_emit_impression_event(name)` | From feature `impressionData` flag |

Evaluation semantics: all built-in strategies (default, userWithId,
gradualRollout*, flexibleRollout, remoteAddress, applicationHostname), all
constraint operators (IN/NOT_IN, STR_*, NUM_*, DATE_*, SEMVER_*, with
`caseInsensitive` and `inverted`), segments, strategy variants, variant
stickiness/weighting, dependent features, murmur3 normalized hashing.

### Out of scope (dropped)

- **Custom strategies:** `register_custom_strategies` raises
  `NotImplementedError` with a clear message.
- **Impact metrics:** `impact_metrics.py` removed; `send_metrics.py` hooks
  stripped.
- `yggdrasilVersion` field omitted from register/metrics payloads.
- `yggdrasil-engine` removed from runtime deps in `pyproject.toml`
  (retained in dev/test deps only, for the differential test).

## Architecture

New package `UnleashClient/engine/`:

| Module | Responsibility |
|---|---|
| `__init__.py` | `UnleashEngine` facade — the drop-in API above |
| `state.py` | ClientFeatures parsing → internal model; segment resolution; warnings |
| `strategies.py` | Built-in strategy evaluation |
| `constraints.py` | All constraint operators |
| `variants.py` | Variant selection, stickiness, strategy variants, `feature_enabled` |
| `hashing.py` | murmur3 normalized hash (`mmh3` already a dependency) |
| `metrics.py` | Toggle/variant counters and metrics bucket |
| `dependencies.py` | Dependent-features resolution |

The rest of the SDK changes only at import sites:
`UnleashClient/__init__.py`, `connectors/*.py`,
`periodic_tasks/send_metrics.py`, `api/register.py`.

**Seed material:** the pre-Yggdrasil pure-Python engine deleted in commit
`fa88e22` (`git show fa88e22^:UnleashClient/...` — strategies/, constraints/,
variants/). Spec-era ~4.x, so it must be gap-filled for: dependent features,
strategy variants, newer constraint operators, segments, and the current
metrics/impression API shape.

## Performance approach

- No JSON serialization anywhere on the evaluation path; context stays a dict.
- Parse/compile state once in `take_state` (pre-resolve segments, pre-parse
  semver/dates/numbers in constraint values, pre-compute variant weight
  tables) so per-call work is minimal.
- Benchmark harness compares embedded vs ygg on representative flag shapes
  (simple on/off, constrained, flexibleRollout w/ stickiness, variants).

## Risks

- **Subtle semantic drift** (hashing seams, stickiness fallbacks, custom
  stickiness, rounding in weight distribution). Mitigated by gates 1–2.
- **Pure-Python slower than expected.** Mitigated by gate 3; if it fails,
  optimize state compilation before falling back to design changes.
- **v5 seed code misleads** where the spec evolved. Mitigation: spec suite is
  the oracle, not the seed code.

## Execution

The migration is executed by a multi-agent Workflow with phases:

1. **Baseline** — spec suite green on yggdrasil; capture benchmark baseline.
2. **Understand** — parallel agents: ygg API semantics map; v5 code
   gap-analysis vs spec 6.1.0; spec-suite area inventory.
3. **Implement** — scaffold interfaces, then parallel per-module agents
   (file-disjoint, TDD against matching spec JSONs).
4. **Integrate** — swap imports, strip dropped capabilities, update
   `pyproject.toml`.
5. **Verify** — spec suite + unit tests + differential fuzz + benchmark gate.
6. **Adversarial review** — parity-gap hunters, silent-failure hunter,
   perf review.
