# 0005: Machine-wide scheduling of timed and system-holding work

- **Status:** Accepted (2026-11-25)
- **Owner:** Craig Hutchinson
- **Relates to:** [ADR 0004](0004-sub0perf.md); Sub0Perf issues 3, 4, 7 and 8; Sub0Platform issues 1 and 2; Sub0LlmEngine `supervision/machine_lease` and its family responsibilities review (decision D4, serving and timing, approved 2026-11-24)

## Context

Timed work on one machine is not uniform. Some jobs are tuning-sensitive and need the machine to themselves; a local LLM engine needs the whole
system (cores, memory bandwidth, GPU) while it runs. Today there are two blunt, unrelated locks: Sub0Perf's `PerfLock` is per repository
(`.git/sub0perf/lock`), and Sub0LlmEngine has a Python-only per-user lease (an exclusive Platform `FileLock` on `<state dir>/sub0/machine.lock`,
with an owner record `pid`, `time`, `what`, `tool` beside it). They do not see each other, and concurrent gates in different repositories disturbed
one another's measurements twice on 2026-11-25 (false confirmed regressions). Every project either waits for everything or risks a bad measurement.

## Decision

1. **One lease, owned by Sub0Platform as mechanism, used by Sub0Perf as policy.** This was already decided in the family review (D4) and in
   Sub0Platform issue 2; this ADR records it. Sub0Platform gets a `lease` module (Python and C++) that lifts the engine's implementation: a per-user
   state directory helper in C++ (Python's `dirs.state` exists), a shared mode on `FileLock` (many shared holders, or one exclusive holder, never
   both), the lock path, the owner record and the "name the holder" message. Sub0Perf's `PerfLock` is nested inside the lease, not beside it. There
   is one lock file on a machine, so the Python and C++ owner records cannot drift.
2. **Two lease modes, `shared` and `exclusive`; nothing else is a class.**
   - `exclusive`: no other holder of any mode, timing or not. Gates, `auto`, `compare`, workload runs and an engine's `fit` and `bench` take this.
   - `shared`: any number of shared holders, and no exclusive one. A serving engine takes this while it answers requests, so a timing run takes the
     lease exclusively and waits for the server to be stopped on purpose. Until Sub0Platform has the shared mode, serving holds nothing and timing
     tools refuse while the engine answers (today's behaviour).
   - There is no `hold-system` class: at the lock, `exclusive` already excludes every other holder, shared ones included, so it would be `exclusive`
     plus a list of resources. And **`parallel-eligible` is not a lease class**: a parallel benchmark set runs inside one `sub0perf` process that
     already holds the lease exclusively and pins its own rows to cores (the issue 8 contract), so no cross-process slot accounting is needed.
3. **The owner record** carries `pid`, `time`, `what`, `tool`, `mode` (`shared` or `exclusive`) and, for reporting only, the resources the holder
   uses (cores, memory bandwidth, GPU, power state). The blocked message names them. **A resource grant-and-refuse registry is deferred** until a
   second kind of shared holder needs it; version 1 only reports.
4. **A blocked job says who holds what**, since when, in which mode, and what would unblock it (never a bare timeout): one human line, and a
   `blocked` object in `sub0perf-outcome-1` (an added key). **The wait policy is bounded and never silent:**
   - Interactive commands (`compare`, `auto`, the CMake targets, an engine's `fit` or `bench`) announce the holder and wait up to the existing
     15 minutes, then fail naming it; `--no-wait` fails at once. This keeps `PerfLock`'s announce-and-wait.
   - The **pre-push gate** waits at most 120 s, then **defers** like the battery rule: the push proceeds, the range goes to
     `.git/sub0perf/deferred.json`, and the next gate measures it. `SUB0PERF_LEASE_STRICT=1` (the twin of `SUB0PERF_POWER_STRICT`) refuses the
     push instead. A busy machine then never blocks a push and never lets an unmeasured change through silently.
   - A shared holder takes the lease at once unless an exclusive holder has it; then it waits like an interactive command, so a timing run in
     progress is never interrupted. A waiting job never starves an exclusive one.
5. **Safety**: a lease expires when its holder dies (Sub0Platform process liveness, the stale-lock rule `ExclusiveLock` already has).
   **Correctness runs (smoke, metrics) never need a lease.**
6. **Parallel rows (issue 8)** run inside the exclusive holder under the contract in the extraction plan (7.6 step 5): declared by the author,
   default serial, measured safe per host, pinned to cores, cross-checked against a serial run, baselines always serial. Correctness before timing
   (item 1 of that contract) does not depend on this ADR and shipped on its own (Sub0Perf PR 17).

## The three consumer cases

| Case | Mode | What the user sees when blocked |
|---|---|---|
| A quick pre-push gate run | `exclusive` | waits up to 120 s: `blocked: held by <tool> (<what>), pid, since <time>, exclusive; waiting`, then defers (or refuses under `SUB0PERF_LEASE_STRICT=1`) |
| A parallel benchmark set | `exclusive`, one process | the same as any exclusive run; its rows run serially until the host profile proves a width safe |
| A serving engine, and an engine fit or bench run | `shared` for serving, `exclusive` for fit and bench | a timing tool waits or refuses naming the engine; the engine's `fit` and `bench` wait for a running timing job |

## Consequences

- Sub0Platform issue 2 becomes the first build step (C++ state directory, shared lock mode, `lease` module, conformance vectors for the owner
  record); Sub0Perf adopts it after (issue 7), then Sub0Squad (host guard), Sub0LlmEngine (fit, bench, serve) and Sub0Canvas timing.
- The lease and the owner record become a cross-repository contract: a change to either names every consumer, like the `sub0perf-*` schemas.
- Sub0Perf stays standard-library Python over Sub0Platform mechanism; implementing the scheduler on Sub0Fabric or Sub0Log would invert the stack
  and is a separate ADR, to be decided once real consumers have used the interface.

## Decisions taken from the open questions (2026-11-25)

1. The lease is Sub0Platform's (the review's D4, already approved), not a convention each consumer implements.
2. `--wait` is on by default, bounded: interactive commands wait up to 15 minutes, the pre-push gate up to 120 s and then defers; `--no-wait` and
   `SUB0PERF_LEASE_STRICT=1` are the two strict forms.
3. Serving holds the lease shared (the review's section 6, decision 2), and timing takes it exclusive.
