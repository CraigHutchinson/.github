# 0005: Machine-wide scheduling of timed and system-holding work

- **Status:** Proposed (2026-11-25); the owner accepts or changes it. Nothing in Sub0Perf issue 3 steps 2-6 or issue 8 is built before it is accepted.
- **Owner:** Craig Hutchinson
- **Relates to:** [ADR 0004](0004-sub0perf.md); Sub0Perf issues 3, 4, 7 and 8; Sub0Platform issues 1 and 2; Sub0LlmEngine `supervision/machine_lease`

## Context

Timed work on one machine is not uniform. Some jobs are minor and can run side by side; some are tuning-sensitive and need the machine to
themselves; a local LLM engine needs the whole system (cores, memory bandwidth, GPU) for as long as it runs. Today there are two blunt, unrelated
locks: Sub0Perf's `PerfLock` is per repository (`.git/sub0perf/lock`), and Sub0LlmEngine has a Python-only per-user lease (an exclusive Platform
`FileLock` on `<state dir>/sub0/machine.lock`, with an owner record `pid`, `time`, `what`, `tool` beside it). They do not see each other, so a
gate run and an engine run can disturb one another, and every project either waits for everything or risks a bad measurement.

## Decision (proposed)

1. **One lease, owned by Sub0Platform as mechanism, used by Sub0Perf as policy.** Sub0Platform gets a `lease` module (Python and C++) that
   lifts the engine's implementation: a per-user state directory helper in C++ (Python's `dirs.state` exists), a shared mode on `FileLock`
   (many shared holders, or one exclusive holder, never both), the lock path, the owner record (`pid`, `time`, `what`, `tool`, plus `class`) and
   the "name the holder" message. Sub0Perf's `PerfLock` is kept inside it, not beside it. There is one lock file on a machine.
2. **Declared class per job**, set by the author and carried in the owner record:
   - `shared`: any number may run, and they hold the lease shared.
   - `parallel-eligible`: may run beside a bounded number of peers (declared width and core-pinning needs, then measured safe per host: issue 8).
   - `exclusive`: tuning-sensitive; no other timed work, and none of the above starts meanwhile. Gates, `auto` and workload runs take this.
   - `hold-system`: everything else is stopped for the whole run, including GPU- and memory-heavy peers (a serving engine, a fit run).
3. **Resources are data, not code**: cores (performance and efficiency, SMT siblings; Sub0Platform `topology`, issue 4), memory bandwidth, GPU,
   power state. A job states what it needs; the scheduler grants or refuses. Classes and resources are a small registry in the consumer's config,
   and an unknown class degrades to `exclusive`, so a project adds its own without a Sub0Perf change.
4. **A blocked job says who holds what**, since when, for which class, and what would unblock it (never a bare timeout): one human line, and a
   `blocked` object in `sub0perf-outcome-1` (an added key). Default behaviour is fail-fast for the pre-push gate, with `--wait [timeout]`
   available; a waiting job never starves an exclusive one.
5. **Safety**: a lease expires when its holder dies (Sub0Platform process liveness, the stale-lock rule `ExclusiveLock` already has); a
   `hold-system` lease cannot be taken while a timed job runs unless the caller waits. **Correctness runs (smoke, metrics) never need a lease.**
6. **Parallel rows (issue 8)** are the `parallel-eligible` class under the contract in the extraction plan (7.6 step 5): declared by the author,
   default serial, measured safe per host, scheduled with pinning and a serial cross-check, baselines always serial. Correctness before timing
   (item 1 of that contract) does not depend on this ADR and shipped on its own (Sub0Perf PR 17).

## The three consumer cases

| Case | Class | Resources | What the user sees when blocked |
|---|---|---|---|
| A quick pre-push gate run | `exclusive` | all timed cores, power state | `blocked: held by <tool> (<what>), pid, since <time>, class exclusive; wait with --wait or stop it` |
| A parallel benchmark set | `parallel-eligible` ×k | k pinned core sets, no SMT siblings paired | the set runs serially until the host profile proves width k safe |
| A serving or fitting engine run | `hold-system` | cores, memory bandwidth, GPU | gate and timing tools refuse while it answers, naming the engine; the engine holds the lease shared while serving so a timing run waits on purpose |

## Consequences

- Sub0Platform issue 2 becomes the first build step (C++ state directory, shared lock mode, `lease` module); Sub0Perf adopts it after (issue 7),
  then Sub0Squad (host guard), Sub0LlmEngine (fit, bench, serve) and Sub0Canvas timing.
- The lease and the owner record become a cross-repository contract: a change to either names every consumer, like the `sub0perf-*` schemas.
- Sub0Perf stays standard-library Python over Sub0Platform mechanism; implementing the scheduler on Sub0Fabric or Sub0Log would invert the stack
  and is a separate ADR, to be decided once real consumers have used the interface.

## Open questions for the owner

1. Is the lease Sub0Platform's (recommended, so Python and C++ cannot drift) or a convention each consumer implements?
2. Is `--wait` off by default for every class, or on for `shared`?
3. Do the engine's serving runs hold the lease shared, or stay outside it until the engine moves to the Platform module?
