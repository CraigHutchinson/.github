# 0004: Sub0Perf, the performance tooling in its own repository

- **Status:** Accepted (2026-11-24); Sub0Perf and Sub0Math's shim are merged, the consumers follow
- **Owner:** Craig Hutchinson
- **Supersedes:** [ADR 0001](0001-sub0math.md) decision 5 (tooling at the bottom of the stack, in Sub0Math)

## Context

Sub0Math's scope is value types and their algebra, but it also carries the family's performance tooling:
about 5,700 lines of Python and CMake in `tools/perf/` and `cmake/Sub0MathTooling.cmake`. Three of the four
consumers (Sub0Canvas, Sub0Fabric, Sub0Squad) download Sub0Math only for that tooling, never for the maths.
Only Sub0Physics uses both. Every tooling fix moves Sub0Math's history and every consumer's Sub0Math pin.
ADR 0001 decision 5 put the tooling "at the bottom of the stack". The stack now has Sub0Platform below
Sub0Math, and the tooling has outgrown that decision.

## Decision

1. **New repository CraigHutchinson/Sub0Perf**, public, MIT. It sits above Sub0Platform and below every library
   that benchmarks. Not in Sub0Platform: the family responsibilities review keeps Platform to mechanism only
   (processes, locks, directories, host facts). sub0perf is policy: statistics, verdicts, baselines, gates, hooks.
2. **Move first, rename later.** Every public name is kept byte for byte: `sub0math_*` CMake functions,
   `SUB0MATH_*` variables, `sub0math_tooling_dir.txt`, the `.git/sub0perf/` state directory, the `bench_check.py`
   command line and the `sub0perf-*` JSON schemas. Renaming to `sub0perf_*` / `SUB0PERF_*` with aliases is a
   separate later change.
3. **Layout keeps the relative paths** consumers and the shared CI tools look for: `tools/perf/**` stays
   `tools/perf/**`, and `bench_runner.hpp` stays at `tools/perf/bench_runner.hpp`. Only
   `cmake/Sub0MathTooling.cmake` (now `cmake/Sub0Perf.cmake`) and `tests/vendor/nanobench.h`
   (now `vendor/nanobench.h`) move.
4. **`sub0math_target_deterministic_fp` stays in Sub0Math** (`cmake/Sub0MathFp.cmake`): it is Sub0Math's
   determinism contract. Sub0Perf keeps a guarded copy until the rename. The test-tier helpers
   (`sub0math_add_test`, `sub0math_label_full_tests`) move.
5. **Transition shim.** Sub0Math keeps `cmake/Sub0MathTooling.cmake` for one release: it prints a deprecation
   message, fetches Sub0Perf at a pinned SHA through CPM when `Sub0Perf_SOURCE_DIR` is unset, and includes
   Sub0Perf's file. Consumers pinned to older Sub0Math SHAs are unaffected until they bump. The shim is removed
   once every consumer has moved.
6. **Sub0Math consumes Sub0Perf only when it is the top-level project.** A consumer of the maths never fetches
   Sub0Perf because of Sub0Math.
7. **Out of scope:** `tools/wasm/wasm_gate.py` and `tools/provenance/` stay in Sub0Math for now. They are family
   CI tooling and could later move to this repository's `tools/ci`.
8. **Open issues follow the code:** Sub0Math issues 4, 5, 6, 7 and 8 transfer to Sub0Perf. The macOS runner issue
   stays in Sub0Math.
9. **Parallel benchmark rows (issue 8) are designed and built in Sub0Perf after the move**, never during it
   (decision 2). Rows stay serial unless a row is both declared parallel-eligible by its author and measured safe on
   the host; the contract is under Follow-ups.

## Follow-ups

- Rename to `sub0perf_*` / `SUB0PERF_*`, reading the old names as fallbacks for one release; same for
  `sub0perf_tooling_dir.txt`.
- Fix the transferred issues on the process-tree and machine-lease handling, with the mechanism parts
  (`platform_proc.py`, the machine lease, power and clock readings) moving to Sub0Platform's Python `host` module.
- Parallel benchmark rows (issue 8), correctness before timing: a row's deterministic metrics and smoke run pass before
  any timing, serial or parallel. Eligibility is declared per row (a `BenchRunner` flag) and per executable
  (`PARALLEL <k>` ceiling, 0 = never); nothing is parallel by default. Safety is measured per host: a topology probe
  (SMT siblings never paired), then per-row interference calibration, safe width the largest k whose median shift stays
  under a third of the gate tolerance without widening the spread, stored in a host profile keyed by host, power state
  and toolchain (a mismatch means serial). `gate`, `auto` and `compare` run safe rows concurrently, each pinned to its
  own cores, cross-check against a serial run on first use and on a sample, and hold the machine lease. Baselines stay
  serial measurements.
- Decide whether `wasm_gate.py` and `tools/provenance/` move to this repository's `tools/ci` (decision 7).
- Set this ADR to Accepted once the Sub0Perf repository is green and Sub0Math consumes it.
