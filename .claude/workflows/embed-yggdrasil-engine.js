export const meta = {
  name: 'embed-yggdrasil-engine',
  description: 'Replace yggdrasil-engine with an embedded pure-Python evaluation engine, spec-tested, differential-tested, and benchmarked faster',
  phases: [
    { title: 'Baseline', detail: 'venv setup, spec suite green on yggdrasil, perf baseline' },
    { title: 'Understand', detail: 'engine API contract, v5 seed gap analysis, spec-suite inventory' },
    { title: 'Scaffold', detail: 'engine package skeleton with frozen interfaces' },
    { title: 'Implement', detail: 'parallel per-module TDD implementation' },
    { title: 'Integrate', detail: 'swap imports, strip dropped capabilities, full suite green' },
    { title: 'Verify', detail: 'differential fuzz vs yggdrasil, benchmark gate, full suite' },
    { title: 'Fix', detail: 'repair failures between verify rounds' },
    { title: 'Review', detail: 'adversarial parity / silent-failure / perf review' },
    { title: 'Finalize', detail: 'docs, changelog, final commit, report' },
  ],
}

const ROOT = '/Users/elyahouittah/connecteam/unleash-python-sdk'
const VENV = ROOT + '/.venv/bin'
const SPEC_DOC = ROOT + '/docs/superpowers/specs/2026-06-03-embedded-engine-design.md'
const CONTRACT_DOC = ROOT + '/docs/superpowers/specs/engine-contract.md'
const GAP_DOC = ROOT + '/docs/superpowers/specs/v5-gap-analysis.md'
const INV_DOC = ROOT + '/docs/superpowers/specs/spec-inventory.md'
const SEED_DIR = ROOT + '/.v5-seed'
const SPECS_DIR = ROOT + '/tests/specification_tests/client-specification/specifications'

const COMMON = `Repo: ${ROOT}, git branch "embedded-engine" (already checked out — stay on it).
Python: use ${VENV}/python and ${VENV}/pytest EXCLUSIVELY (never system python/pip).
When running pytest ALWAYS pass -o addopts='' to disable default coverage/html reporting (it clashes between parallel runs). Example: ${VENV}/pytest -o addopts='' -q tests/unit_tests/engine/test_constraints.py
First read the approved design spec: ${SPEC_DOC}
HARD RULES: never push to any git remote; never run docker; never edit files outside your assigned scope; do not commit unless your instructions explicitly say to.`

// ---------- schemas ----------
const BASELINE_SCHEMA = {
  type: 'object',
  properties: {
    venvReady: { type: 'boolean' },
    specSuitePassed: { type: 'boolean' },
    specTestCount: { type: 'number' },
    baselineFile: { type: 'string', description: 'path to saved yggdrasil benchmark JSON' },
    scenarios: { type: 'object', description: 'scenario -> ops/sec for yggdrasil baseline' },
    notes: { type: 'string' },
  },
  required: ['venvReady', 'specSuitePassed', 'specTestCount', 'baselineFile'],
}

const DOC_SCHEMA = {
  type: 'object',
  properties: {
    docPath: { type: 'string' },
    summary: { type: 'string', description: 'max ~15 lines, the key facts a downstream implementer must not miss' },
  },
  required: ['docPath', 'summary'],
}

const SCAFFOLD_SCHEMA = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string' } },
    interfaces: { type: 'string', description: 'concise listing of every module-level function/class signature an implementer must respect' },
    notes: { type: 'string' },
  },
  required: ['files', 'interfaces'],
}

const MODULE_SCHEMA = {
  type: 'object',
  properties: {
    module: { type: 'string' },
    status: { type: 'string', enum: ['complete', 'partial', 'blocked'] },
    testCount: { type: 'number' },
    testsPassed: { type: 'boolean' },
    interfaceChangeRequests: { type: 'array', items: { type: 'string' }, description: 'shared-interface changes you NEEDED but were not allowed to make' },
    notes: { type: 'string' },
  },
  required: ['module', 'status', 'testsPassed'],
}

const INTEGRATE_SCHEMA = {
  type: 'object',
  properties: {
    unitTestsPassed: { type: 'boolean' },
    specSuitePassed: { type: 'boolean' },
    specTestCount: { type: 'number' },
    yggdrasilRemovedFromRuntimeDeps: { type: 'boolean' },
    committed: { type: 'boolean' },
    notes: { type: 'string' },
  },
  required: ['unitTestsPassed', 'specSuitePassed', 'committed'],
}

const GATE_SCHEMA = {
  type: 'object',
  properties: {
    passed: { type: 'boolean' },
    detail: { type: 'string', description: 'if failed: exact failing tests/mismatches/numbers, enough for another agent to fix without re-running discovery' },
  },
  required: ['passed', 'detail'],
}

const BENCH_SCHEMA = {
  type: 'object',
  properties: {
    passed: { type: 'boolean', description: 'true iff embedded engine is faster overall (geomean speedup > 1.0)' },
    geomeanSpeedup: { type: 'number' },
    scenarios: { type: 'object', description: 'scenario -> {yggdrasil_ops_sec, embedded_ops_sec, speedup}' },
    detail: { type: 'string' },
  },
  required: ['passed', 'geomeanSpeedup', 'detail'],
}

const FINDINGS_SCHEMA = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          title: { type: 'string' },
          file: { type: 'string' },
          detail: { type: 'string', description: 'concrete: what input produces wrong/slow behavior and why' },
          severity: { type: 'string', enum: ['critical', 'major', 'minor'] },
        },
        required: ['title', 'file', 'detail', 'severity'],
      },
    },
  },
  required: ['findings'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    isReal: { type: 'boolean' },
    evidence: { type: 'string', description: 'repro command + observed output, or the reasoning that refutes it' },
  },
  required: ['isReal', 'evidence'],
}

// ---------- Phase 1: Baseline ----------
phase('Baseline')
log('Setting up venv, validating yggdrasil baseline, recording perf baseline')
const baseline = await agent(`${COMMON}

You are the BASELINE agent. Tasks, in order:
1. Create the venv if missing: python3 -m venv ${ROOT}/.venv
2. ${VENV}/pip install -U -r ${ROOT}/requirements.txt && ${VENV}/pip install -e ${ROOT}
3. Download the client specification (the script needs the venv python on PATH):
   cd ${ROOT} && PATH=${VENV}:$PATH ./scripts/get-spec.sh
4. Run the official spec suite against the CURRENT yggdrasil-based SDK:
   cd ${ROOT} && ${VENV}/pytest -o addopts='' -q tests/specification_tests
   It must be 100% green. Record the test count. If it is not green, STOP and report failure details.
5. Create ${ROOT}/benchmarks/bench_engine.py — a standalone script (python bench_engine.py [--engine yggdrasil|embedded] [--out FILE]) that:
   - Builds ONE representative ClientFeatures JSON state containing these scenarios as separate toggles: (a) simple always-on toggle, (b) toggle with 5 constraints mixing STR_CONTAINS / NUM_GTE / DATE_AFTER / SEMVER_GT / IN, (c) flexibleRollout 50% with userId stickiness, (d) toggle with 4 weighted variants + an override, (e) userWithId strategy with 100 ids.
   - Instantiates the engine (for now only yggdrasil exists: from yggdrasil_engine.engine import UnleashEngine; later --engine embedded will do: from UnleashClient.engine import UnleashEngine — write the import switch now, guarded so it fails cleanly if not yet available), calls take_state(state_json).
   - For each scenario, times a hot loop of >= 50_000 is_enabled calls (and get_variant for the variants scenario) with a realistic context dict ({userId, sessionId, remoteAddress, properties:{semverField, numField, dateField, strField}}), computes ops/sec using time.perf_counter.
   - Writes JSON {engine, python_version, scenarios: {name: ops_per_sec}} to --out.
6. Run it: ${VENV}/python ${ROOT}/benchmarks/bench_engine.py --engine yggdrasil --out ${ROOT}/benchmarks/baseline_yggdrasil.json
7. git add benchmarks/ && git commit -m "bench: yggdrasil baseline harness and numbers"

Return the structured result. Your final text is data for an orchestrator, not prose.`, { schema: BASELINE_SCHEMA, label: 'baseline' })

if (!baseline.specSuitePassed) {
  return { status: 'aborted', reason: 'Spec suite is not green on the yggdrasil baseline — fix that before migrating.', baseline }
}
log(`Baseline green: ${baseline.specTestCount} spec tests, benchmark saved to ${baseline.baselineFile}`)

// ---------- Phase 2: Understand ----------
phase('Understand')
const [contract, gap, inventory] = await parallel([
  () => agent(`${COMMON}

You are the API-CONTRACT agent (read-only on SDK code; you write ONE doc file). Produce the exact behavioral contract of yggdrasil's UnleashEngine as used by this SDK.
Sources:
- Installed wrapper source: ${ROOT}/.venv/lib/python*/site-packages/yggdrasil_engine/ (read every .py file; note where it serializes to JSON and what the FFI returns)
- Every SDK call site: grep -rn "engine\\." ${ROOT}/UnleashClient/
Document for EACH method the SDK uses (take_state, get_state, is_enabled, get_variant, list_known_toggles, count_toggle, count_variant, get_metrics, should_emit_impression_event — also note register_custom_strategies and the impact-metrics methods as DROPPED per the design spec):
- exact signature and argument types
- exact return type and shape: especially (a) the variant object's fields and how UnleashClient/__init__.py consumes it via asdict() and dict access (name, enabled, feature_enabled, payload), (b) the metrics bucket JSON shape ({start, stop, toggles: {name: {yes, no, variants: {...}}}} — verify exactly), (c) take_state's warnings structure, (d) what is_enabled returns for an UNKNOWN toggle (the SDK treats None as 'use fallback' — confirm), (e) what get_variant returns for unknown/disabled toggles
- error semantics and None-handling
- context dict handling: how top-level fields vs properties are passed (see _safe_context in UnleashClient/__init__.py) and what the engine expects
- custom stickiness behavior, default stickiness fallback
Write the contract as markdown to ${CONTRACT_DOC} (target 150-350 lines, precise over verbose). Do NOT commit.`, { schema: DOC_SCHEMA, label: 'api-contract', phase: 'Understand' }),

  () => agent(`${COMMON}

You are the V5-SEED agent. The pure-Python engine that predates yggdrasil was deleted in commit fa88e22. Recover and analyze it:
1. mkdir -p ${SEED_DIR} && cd ${ROOT} && for f in $(git ls-tree -r --name-only fa88e22^ -- UnleashClient); do mkdir -p ${SEED_DIR}/$(dirname $f); git show fa88e22^:$f > ${SEED_DIR}/$f; done
2. Append ".v5-seed/" to ${ROOT}/.gitignore (only if not already present).
3. Read the recovered strategies/, constraints/, variants/, features/, loader.py code.
4. Write a gap analysis to ${GAP_DOC}: for each engine concern (hashing, each strategy, each constraint operator, variant selection/stickiness, segments, strategy variants, dependent features, metrics, impression events) state: REUSABLE-AS-IS / REUSABLE-WITH-CHANGES (say which) / MISSING-IMPLEMENT-FRESH. The v5 code targets client-spec ~4.x; current target is 6.1.0 — be suspicious of: variant hashing seed differences, custom stickiness, segments, strategy variants, dependent features, newer constraint operators (semver/date/num, caseInsensitive, inverted).
Do NOT commit.`, { schema: DOC_SCHEMA, label: 'v5-gap-analysis', phase: 'Understand' }),

  () => agent(`${COMMON}

You are the SPEC-INVENTORY agent (read-only; you write ONE doc file). The official client-specification v6.1.0 is at ${SPECS_DIR} (index.json lists all spec files).
Read index.json and skim every spec JSON. Write ${INV_DOC} containing:
1. A table: spec file -> which engine module(s) it exercises (constraints.py / strategies.py / variants.py / state.py / dependencies.py / hashing) -> notable edge cases it pins (e.g. missing context field, inverted constraints, custom stickiness, variant override precedence, dependent-feature variant conditions, segment references).
2. The complete list of constraint operators, strategy names, and stickiness values that appear anywhere in the suite.
3. Any spec areas relying on behavior beyond pure evaluation (e.g. toggles with impressionData).
Do NOT commit.`, { schema: DOC_SCHEMA, label: 'spec-inventory', phase: 'Understand' }),
])
log('Understanding docs written: contract, gap analysis, spec inventory')

// ---------- Phase 3: Scaffold ----------
phase('Scaffold')
const scaffold = await agent(`${COMMON}

You are the SCAFFOLD agent. Read these first:
- ${CONTRACT_DOC} (exact API contract — your facade must match it)
- ${GAP_DOC}
- ${INV_DOC}
Create the new package ${ROOT}/UnleashClient/engine/ with these files, containing COMPLETE interfaces (dataclasses, type hints, docstrings, function signatures) but bodies that raise NotImplementedError (except trivial pure data classes which you fully implement):
- __init__.py: class UnleashEngine — drop-in replacement implementing exactly the contract: take_state, get_state, is_enabled, get_variant, list_known_toggles, count_toggle, count_variant, get_metrics, should_emit_impression_event. register_custom_strategies must raise NotImplementedError('Custom strategies are not supported by the embedded engine'). The facade delegates to the modules below; write the delegation wiring now so only module internals remain.
- state.py: internal model dataclasses (FeatureState, StrategyDef, ConstraintDef, VariantDef, Segment, ...) + parse_client_features(json_str) -> (EngineState, warnings) + serialize_state(EngineState) -> str. Design for PRE-COMPILATION: constraint values parsed once (semver/date/float), segment refs resolved once, variant weight tables precomputed — per the perf section of the design spec.
- constraints.py: evaluate_constraint(constraint, context) -> bool (+ whatever pure helpers you define)
- strategies.py: evaluate_strategy(strategy, context, state) -> bool dispatch + one function per built-in strategy
- variants.py: select_variant(feature, context, state) -> Optional[Variant] handling feature variants, strategy variants, stickiness, overrides
- hashing.py: normalized_hash(group, identifier, normalizer, seed=0) -> int using mmh3 (already a dependency)
- metrics.py: MetricsBucket class: count_toggle/count_variant/snapshot-and-reset matching the bucket shape in the contract
- dependencies.py: parent/dependent-feature resolution
Also create tests/unit_tests/engine/__init__.py (empty) and verify: ${VENV}/python -c "from UnleashClient.engine import UnleashEngine; UnleashEngine()" runs without error (constructor must not raise).
Interfaces you define here are FROZEN for the parallel implementers — make them precise and minimal. Run ${VENV}/python -m compileall on the package. Then git add UnleashClient/engine tests/unit_tests/engine && git commit -m "feat: scaffold embedded engine package".
Return every file created and a concise listing of all module-level signatures.`, { schema: SCAFFOLD_SCHEMA, label: 'scaffold' })
log('Scaffold committed; interfaces frozen')

// ---------- Phase 4: Implement (parallel, file-disjoint) ----------
phase('Implement')
const MODULES = [
  {
    name: 'constraints',
    own: ['UnleashClient/engine/constraints.py', 'tests/unit_tests/engine/test_constraints.py'],
    focus: `All constraint operators: IN/NOT_IN, STR_CONTAINS/STR_STARTS_WITH/STR_ENDS_WITH, NUM_EQ/NUM_GT/NUM_GTE/NUM_LT/NUM_LTE, DATE_AFTER/DATE_BEFORE, SEMVER_EQ/SEMVER_GT/SEMVER_LT. Honor caseInsensitive and inverted flags, single "value" vs list "values", context field lookup across top-level fields AND properties (see contract doc), currentTime handling for DATE_*. Unparseable numeric/semver/date context values must evaluate the constraint to false (then inverted applies per spec — check spec tests for exact order). Use the "semver" and "python-dateutil" packages already in deps. Pre-parse constraint-side values at state-load time where the interface allows.`,
  },
  {
    name: 'hashing-variants',
    own: ['UnleashClient/engine/hashing.py', 'UnleashClient/engine/variants.py', 'tests/unit_tests/engine/test_hashing.py', 'tests/unit_tests/engine/test_variants.py'],
    focus: `hashing.py: normalized murmur3 hash via mmh3: mmh3.hash("group:identifier", seed, signed=False) % normalizer + 1 — VERIFY exact formula against spec tests (08-gradual-rollout + variant specs). CRITICAL: variant distribution uses a DIFFERENT hash seed than gradual rollout — derive the exact seeds from the client-spec test expectations; do not guess. variants.py: weighted selection, overrides take precedence, stickiness (default = userId -> sessionId -> random fallback; custom stickiness field; "random" stickiness), strategy-level variants vs feature-level variants, payload passthrough, feature_enabled flag on returned variant, disabled-variant result for missing/disabled features per the contract doc.`,
  },
  {
    name: 'strategies',
    own: ['UnleashClient/engine/strategies.py', 'tests/unit_tests/engine/test_strategies.py'],
    focus: `Built-in strategies: default, userWithId, gradualRolloutUserId, gradualRolloutSessionId, gradualRolloutRandom, flexibleRollout (stickiness default/userId/sessionId/random/custom-field; groupId defaults to feature name), remoteAddress (single IPs AND CIDR ranges — use stdlib ipaddress; tolerate invalid addresses as non-match), applicationHostname. Strategy passes only if its constraints pass (call the frozen constraints interface; in unit tests monkeypatch it since constraints is implemented in parallel) AND segment-referenced constraints pass (segments resolved by state.py — respect the frozen interface). A strategy referencing a NONEXISTENT segment must make the toggle evaluate to false (spec 15-global-constraints pins this). Empty strategy list semantics: check contract doc.`,
  },
  {
    name: 'state-dependencies',
    own: ['UnleashClient/engine/state.py', 'UnleashClient/engine/dependencies.py', 'tests/unit_tests/engine/test_state.py', 'tests/unit_tests/engine/test_dependencies.py'],
    focus: `state.py: parse ClientFeatures JSON ({version, features:[], segments:[]}), build the pre-compiled internal model (pre-parse constraint values, resolve nothing lazily on the hot path), produce per-feature warnings for unparseable entries WITHOUT failing the whole payload (match take_state contract), serialize_state round-trips losslessly enough that take_state(get_state()) yields identical evaluation (the streaming connector relies on this: see UnleashClient/connectors/streaming_connector.py). list_known_toggles shape per contract. dependencies.py: dependent features per spec 14-dependent-features: parent must be enabled (or "enabled: false" expectation), optional required variants, missing parent => false, dependencies are NOT transitive for metrics-counting of parents (check spec).`,
  },
  {
    name: 'metrics',
    own: ['UnleashClient/engine/metrics.py', 'tests/unit_tests/engine/test_metrics.py'],
    focus: `MetricsBucket matching the contract: per-toggle yes/no counts, per-variant counts, bucket {start, stop, toggles} with UTC ISO timestamps, get_metrics snapshots AND resets atomically (thread-safety: the SDK calls count_toggle from request threads and get_metrics from the apscheduler thread — use a lock), returns None/empty per contract when no activity (send_metrics checks truthiness). should_emit_impression_event(name) from the feature's impressionData flag (false for unknown features).`,
  },
]

const implResults = await parallel(MODULES.map(m => () => agent(`${COMMON}

You are the ${m.name.toUpperCase()} implementer. Other agents are implementing sibling modules IN PARALLEL right now.
FILES YOU OWN (the ONLY source files you may create/edit):
${m.own.map(f => '- ' + ROOT + '/' + f).join('\n')}
FROZEN INTERFACES (respect exactly; if a shared interface is genuinely wrong, do NOT change it — record it in interfaceChangeRequests):
${scaffold.interfaces}

Required reading before coding: ${CONTRACT_DOC}, ${INV_DOC}, the relevant spec JSONs under ${SPECS_DIR}, ${GAP_DOC} and the matching v5 seed code under ${SEED_DIR}/UnleashClient (reuse where the gap analysis says REUSABLE, but the spec suite is the oracle — never trust seed code over a spec test).

Focus and edge cases: ${m.focus}

Method: TDD. Write unit tests in your owned test file(s) FIRST, deriving cases directly from the client-spec JSONs (embed the relevant state snippets in the test file; test your module's functions directly, not the full client). Then implement until green:
${VENV}/pytest -o addopts='' -q ${m.own.filter(f => f.includes('test_')).map(f => ROOT + '/' + f).join(' ')}
Performance matters (design spec): no JSON serialization, no per-call re-parsing, no regex compilation in hot paths.
Do NOT commit. Do NOT run the full test suite (other modules are mid-flight).`, { schema: MODULE_SCHEMA, label: `impl:${m.name}`, phase: 'Implement' })))

const impl = implResults.filter(Boolean)
const blocked = impl.filter(r => r.status === 'blocked' || !r.testsPassed)
const changeRequests = impl.flatMap(r => (r.interfaceChangeRequests || []).map(c => `[${r.module}] ${c}`))
log(`Implementation: ${impl.length}/${MODULES.length} modules done, ${blocked.length} blocked/failing, ${changeRequests.length} interface change requests`)

// ---------- Phase 5: Integrate ----------
phase('Integrate')
const integration = await agent(`${COMMON}

You are the INTEGRATION agent. The embedded engine modules are implemented. Your job: make the whole SDK run on it and get everything green.
Module reports: ${JSON.stringify(impl.map(r => ({ module: r.module, status: r.status, testsPassed: r.testsPassed, notes: r.notes })))}
Interface change requests to reconcile (you MAY now change shared interfaces coherently across all engine files): ${JSON.stringify(changeRequests)}

Tasks:
1. Resolve blocked/failing modules and interface change requests; get ALL engine unit tests green: ${VENV}/pytest -o addopts='' -q tests/unit_tests/engine
2. Swap the SDK onto the embedded engine — every import site: UnleashClient/__init__.py, UnleashClient/connectors/*.py, UnleashClient/periodic_tasks/send_metrics.py, UnleashClient/api/register.py, UnleashClient/impact_metrics.py. Replace "from yggdrasil_engine.engine import UnleashEngine" with "from UnleashClient.engine import UnleashEngine".
3. Strip dropped capabilities per the design spec: delete UnleashClient/impact_metrics.py and all its wiring (grep for impact_metrics, define_counter, inc_counter, define_gauge, set_gauge, define_histogram, observe_histogram, collect_impact_metrics, restore_impact_metrics — including in __init__.py public API and send_metrics.py); remove their tests. Custom strategies: UnleashClient constructor must raise ValueError with a clear message when custom_strategies is passed non-empty; remove/update custom-strategy tests accordingly. Remove the yggdrasilVersion field from register and metrics payloads (api/register.py, periodic_tasks/send_metrics.py) and any other "import yggdrasil_engine" left anywhere in UnleashClient/.
4. Dependencies: remove "yggdrasil-engine" from pyproject.toml [project] dependencies. In requirements.txt move it under the Development packages section with comment "# differential testing vs embedded engine only". Do NOT uninstall it from the venv (the verify phase needs it).
5. Full validation loop until green:
   ${VENV}/pytest -o addopts='' -q tests/unit_tests
   ${VENV}/pytest -o addopts='' -q tests/specification_tests
   (Update any unit tests that reference dropped capabilities; NEVER weaken a spec test.)
6. Lint: ${VENV}/python -m black ${ROOT}/UnleashClient ${ROOT}/tests && ${VENV}/python -m ruff check ${ROOT}/UnleashClient ${ROOT}/tests --fix (if black/ruff are missing from the venv, pip install them into it).
7. Commit EVERYTHING (including the parallel implementers' uncommitted work) as: "feat: embed pure-python evaluation engine, drop yggdrasil runtime dependency"
Report counts and status truthfully — if the spec suite is not 100% green, say so with the failing test names in notes.`, { schema: INTEGRATE_SCHEMA, label: 'integrate' })
log(`Integration: unit=${integration.unitTestsPassed} spec=${integration.specSuitePassed} (${integration.specTestCount} spec tests)`)

// ---------- Phase 6: Verify (loop with Fix, max 3 rounds) ----------
const DIFF_PROMPT = `${COMMON}

You are the DIFFERENTIAL-FUZZ gate. Create (or update) ${ROOT}/tests/differential/test_differential.py:
- pytest.importorskip("yggdrasil_engine") at top (it is dev-only now; the venv has it).
- A deterministic generator (python random.Random(42)) producing ~200 ClientFeatures states covering: every built-in strategy, every constraint operator (valid AND malformed values), segments (incl. dangling segment refs), feature variants + strategy variants + overrides, custom/random stickiness, dependent features, impressionData flags.
- For each state: feed the SAME JSON to yggdrasil's UnleashEngine and to UnleashClient.engine.UnleashEngine via take_state; then for ~50 generated contexts (mix of full/partial/empty/odd-typed values, all values stringified the way _safe_context does) assert identical is_enabled(name, ctx) and identical get_variant(name, ctx) (compare as plain dicts; normalize ygg's dataclass via dataclasses.asdict and drop None fields, mirroring UnleashClient._resolve_variant).
- Run: ${VENV}/pytest -o addopts='' -q tests/differential -x
If mismatches: report EVERY distinct mismatch (state snippet + context + both results) in detail — do NOT fix engine code yourself. If green: git add tests/differential && git commit -m "test: differential fuzz vs yggdrasil".`

const BENCH_PROMPT = `${COMMON}

You are the BENCHMARK gate. Run the head-to-head:
1. ${VENV}/python ${ROOT}/benchmarks/bench_engine.py --engine embedded --out ${ROOT}/benchmarks/results_embedded.json (fix the import switch in the harness if needed — the embedded engine is UnleashClient.engine.UnleashEngine — but do NOT change scenarios or iteration counts).
2. Re-run the yggdrasil side fresh for fairness on this machine: ${VENV}/python ${ROOT}/benchmarks/bench_engine.py --engine yggdrasil --out ${ROOT}/benchmarks/baseline_yggdrasil.json
3. Compute per-scenario speedup (embedded/yggdrasil) and the geometric mean. PASS iff geomean > 1.0.
4. If PASS: git add benchmarks && git commit -m "bench: embedded vs yggdrasil head-to-head". If FAIL: profile the slowest scenario (${VENV}/python -m cProfile -s cumtime on a small loop) and put the top hot spots in detail — do NOT fix engine code yourself.`

const SUITE_PROMPT = `${COMMON}

You are the FULL-SUITE gate. Run, in order, reporting exact failures if any (do NOT fix anything):
1. ${VENV}/pytest -o addopts='' -q tests/unit_tests
2. ${VENV}/pytest -o addopts='' -q tests/specification_tests
3. ${VENV}/python -c "import UnleashClient; c = UnleashClient.UnleashClient(url='http://x', app_name='smoke', disable_metrics=True, disable_registration=True)" (smoke import/构造 check)
4. grep -rn "yggdrasil" ${ROOT}/UnleashClient/ — must return NOTHING (report any hits).
PASS iff all four are clean.`

let verifyRound = 0
let gates = { diff: null, bench: null, suite: null }
let allGreen = false
while (verifyRound < 3 && !allGreen) {
  verifyRound++
  log(`Verify round ${verifyRound}`)
  const [diff, bench, suite] = await parallel([
    () => agent(DIFF_PROMPT, { schema: GATE_SCHEMA, label: `verify:differential r${verifyRound}`, phase: 'Verify' }),
    () => agent(BENCH_PROMPT, { schema: BENCH_SCHEMA, label: `verify:benchmark r${verifyRound}`, phase: 'Verify' }),
    () => agent(SUITE_PROMPT, { schema: GATE_SCHEMA, label: `verify:full-suite r${verifyRound}`, phase: 'Verify' }),
  ])
  gates = { diff, bench, suite }
  const failures = []
  if (!diff || !diff.passed) failures.push({ gate: 'differential', detail: diff ? diff.detail : 'agent failed' })
  if (!bench || !bench.passed) failures.push({ gate: 'benchmark', detail: bench ? bench.detail : 'agent failed' })
  if (!suite || !suite.passed) failures.push({ gate: 'full-suite', detail: suite ? suite.detail : 'agent failed' })
  if (failures.length === 0) { allGreen = true; break }
  log(`Round ${verifyRound}: ${failures.length} gate(s) failed — dispatching fix agent`)
  await agent(`${COMMON}

You are the FIX agent (round ${verifyRound}). These verification gates failed:
${JSON.stringify(failures, null, 2)}
Fix the engine code under ${ROOT}/UnleashClient/engine/ (and only what is necessary elsewhere). Rules:
- Correctness failures (differential mismatches, failing spec/unit tests): the client-specification and yggdrasil behavior are the oracle — change OUR engine to match, never the tests (except a differential test that itself mis-normalizes results: justify in your notes if so).
- Benchmark failure: optimize using the profiler hot spots in the detail above; favor take_state-time pre-compilation over hot-path work; do not break correctness (re-run engine unit tests + spec suite after optimizing).
- Run the relevant failing checks locally until they pass.
- Commit your fixes: "fix: verify round ${verifyRound} gate repairs".`, { label: `fix r${verifyRound}`, phase: 'Fix' })
}

if (!allGreen) {
  return {
    status: 'failed-gates',
    rounds: verifyRound,
    gates,
    message: 'Verification gates still failing after 3 fix rounds — human review needed. All work is committed on branch embedded-engine.',
  }
}
log('All verification gates green')

// ---------- Phase 7: Adversarial review ----------
phase('Review')
const HUNTERS = [
  {
    key: 'parity',
    prompt: `Hunt for PARITY GAPS between the embedded engine (${ROOT}/UnleashClient/engine/) and Unleash spec/yggdrasil semantics that the spec suite and differential fuzz might MISS. Read the engine code line-by-line and probe with one-off ${VENV}/python scripts. Known treacherous areas: variant hash seed vs rollout hash seed; stickiness fallback order when userId/sessionId absent; custom stickiness referencing properties; weight distribution rounding when weights don't sum to 100/1000; SEMVER pre-release ordering and build-metadata handling; DATE_* timezone-naive vs aware comparison; NUM_* on float-vs-int strings; inverted constraints when the context field is MISSING; case-insensitivity applied to values vs context side; empty values lists; toggles with zero strategies; segment with empty constraints; dependent feature whose parent has variants; get_state round-trip losing fields yggdrasil preserves; thread-safety of take_state swapping state mid-evaluation. Report only findings you can demonstrate with a concrete input.`,
  },
  {
    key: 'silent-failure',
    prompt: `Hunt for SILENT FAILURES in ${ROOT}/UnleashClient/engine/ and the integration diff (git diff main...HEAD): bare/broad excepts that swallow evaluation errors and silently return False/None where yggdrasil would have surfaced a warning; take_state failure paths that leave stale state without logging; metrics lost on exception; warnings list never populated; lock misuse that can deadlock or skip counting. The SDK's contract: evaluation NEVER raises into user code, unknown toggle -> None -> fallback. Verify the engine honors that without ALSO eating real bugs (e.g. TypeError from our own code returning False instead of crashing tests). Report only demonstrated findings.`,
  },
  {
    key: 'perf',
    prompt: `Hunt for PERFORMANCE issues on the hot path (is_enabled/get_variant) in ${ROOT}/UnleashClient/engine/: per-call parsing (dateutil.parse, semver.VersionInfo.parse, float()) of CONSTRAINT-side values that should be pre-compiled in take_state; per-call list/dict allocations that could be precomputed; repeated context property lookups; lock contention in metrics counting on every call (is the critical section minimal?); accidental deep copies. Confirm suspicions with ${VENV}/python -m timeit or cProfile micro-checks. Report only demonstrated findings with measured numbers.`,
  },
]

const reviewResults = await pipeline(
  HUNTERS,
  h => agent(`${COMMON}\n\nYou are an adversarial reviewer (read-only — do NOT edit code).\n${h.prompt}`, { schema: FINDINGS_SCHEMA, label: `hunt:${h.key}`, phase: 'Review' }),
  (res, h) => parallel((res && res.findings ? res.findings : []).map(f => () =>
    agent(`${COMMON}

Adversarially VERIFY this ${h.key} finding about ${ROOT}/UnleashClient/engine/ — your default stance is that it is NOT real; only confirm with reproduced evidence (run actual code with ${VENV}/python). Do not edit any files.
Finding: ${JSON.stringify(f)}`, { schema: VERDICT_SCHEMA, label: `verify:${f.title.slice(0, 40)}`, phase: 'Review' })
      .then(v => ({ ...f, hunter: h.key, verdict: v }))
  ))
)

const confirmed = reviewResults.filter(Boolean).flat().filter(Boolean).filter(f => f.verdict && f.verdict.isReal)
log(`Adversarial review: ${confirmed.length} confirmed finding(s)`)

phase('Finalize')
if (confirmed.length > 0) {
  await agent(`${COMMON}

You are the REVIEW-FIX agent. Fix these CONFIRMED findings (each has reproduced evidence):
${JSON.stringify(confirmed, null, 2)}
After fixing, re-run and require green: engine unit tests, spec suite, differential suite (all with -o addopts=''), and the benchmark must still pass (geomean speedup > 1.0 via benchmarks/bench_engine.py both engines). Commit: "fix: confirmed adversarial review findings".`, { label: 'review-fix', phase: 'Finalize' })
}

const finalReport = await agent(`${COMMON}

You are the FINALIZE agent:
1. Re-run the final gates one last time and record exact numbers: unit tests, spec suite (count), differential suite, benchmark (per-scenario + geomean from benchmarks/*.json).
2. Update README.md: remove/replace claims about Yggdrasil; note custom strategies and impact metrics are not supported in this fork; one short section "Embedded evaluation engine".
3. Add a CHANGELOG.md entry at the top for this fork change (unreleased section): embedded pure-Python engine, yggdrasil-engine removed from runtime deps, dropped custom strategies + impact metrics, perf numbers.
4. ${VENV}/python -m black UnleashClient tests && ${VENV}/python -m ruff check UnleashClient tests --fix; commit everything: "docs: embedded engine fork notes and changelog".
5. git log --oneline main..HEAD for the commit list.
Return: {summary, specTestCount, unitTestsPassed, differentialPassed, geomeanSpeedup, scenarios, commits, caveats} — truthful numbers only, from the runs you just did.`, {
  schema: {
    type: 'object',
    properties: {
      summary: { type: 'string' },
      specTestCount: { type: 'number' },
      unitTestsPassed: { type: 'boolean' },
      differentialPassed: { type: 'boolean' },
      geomeanSpeedup: { type: 'number' },
      scenarios: { type: 'object' },
      commits: { type: 'array', items: { type: 'string' } },
      caveats: { type: 'string' },
    },
    required: ['summary', 'unitTestsPassed', 'differentialPassed', 'geomeanSpeedup', 'commits'],
  },
  label: 'finalize',
})

return {
  status: 'complete',
  baselineScenarios: baseline.scenarios,
  verifyRounds: verifyRound,
  confirmedReviewFindings: confirmed.map(f => f.title),
  final: finalReport,
}