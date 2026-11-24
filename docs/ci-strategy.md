# CI strategy: local-first, tiered, release-complete

- **Status:** gold standard for every repository in the Sub0, Kismo and PhantomFlow family (2026-10-28).
- **Reusable workflows:** `.github/workflows/cpp-smoke.yml` and `cpp-full.yml` in this repository.
- **Local attestation:** `tools/ci/local_ci.py` in this repository.

## Why

Hosted CI was being used as the everyday verifier. One push to a private C++ repository ran eight jobs: a full
platform matrix, sanitizers, a reference simulator build, golden regeneration and a 37-minute performance job on
noisy shared runners. That came to about 100 billable minutes per push, and the organisation's Actions quota ran
out. Almost all of it repeated checks that already run locally before every push:
- the build and quick tests;
- the performance gate (Sub0Perf tooling);
- several local compilers, including an FMA-capable build.

## Principles

1. **Local first.** Whatever a developer machine can verify is verified there: build, quick and full tests on
   the local compilers, the performance gate, determinism digests, and golden data. CI is for what local cannot do.
2. **CI covers what local cannot.**
   - Other operating systems and toolchains: Linux GCC and Clang, MSVC, macOS.
   - A clean-room build from pinned dependencies only, with no local caches or `CPM_*_SOURCE` overrides.
   - Sanitizers.
   - Long reference suites when a release is being cut.
3. **Pay for what you need.** Run the cheap check on every change and the expensive matrix on releases, so there
   is no full platform matrix on every commit.
4. **Nothing is skipped silently.** Every tier says what it covered. A release is never cut without the full
   matrix.

## Tiers

| Tier | Trigger | Jobs | Typical cost |
|---|---|---|---|
| **None** | A change touching only documentation (`**/*.md`, `docs/**`, licence files, provenance manifests) | Nothing (`paths-ignore`) | 0 |
| **Smoke** | Every push to `main` and every pull request | One Linux job: configure, build with warnings as errors, `ctest --preset quick` (long suites are labelled `full` and skipped). Superseded runs are cancelled | About 5 min |
| **Full** | A tag `v*` or `milestone/*`; `workflow_dispatch`; a pull request labelled `ci:full`; a weekly schedule that runs **only if `main` changed since the last successful full run** | Linux GCC and Clang, an FMA leg where determinism matters, Windows MSVC, macOS, sanitizers, `ctest --preset full` including reference suites, provenance and golden regeneration | 30–100 min |
| **Release** | A release tag | The full tier must pass before the release is published | As full |

### Explicit no-CI gate

CI can be switched off deliberately, so it never just fails. For example, when a billing quota is exhausted, the
family continues local-first until the cycle resets.

| Gate | How | Scope |
|---|---|---|
| **`CI_MODE` variable** | Every job in the reusable workflows runs only `if: vars.CI_MODE != 'off'`. Set it per organisation (`gh variable set CI_MODE --org <org> --body off`) or per repository (`gh variable set CI_MODE -R <owner/repo> --body off`). `smoke` limits a repository to the smoke tier; unset or `full` is normal operation | Everything, with no commits needed; reversible with one command |
| **`[skip ci]`** | GitHub's own commit-message marker (`[skip ci]`, `[ci skip]`, `[no ci]`, `[skip actions]`): the push or pull request runs no workflows | One commit: comment-only, formatting, metadata or other changes that cannot affect a build |
| **`ci:skip` label** | Pull requests labelled `ci:skip` run no tier. `ci-policy` then requires a `local-ci/quick` or `local-ci/full` status instead | One pull request |
| **Documentation only** | `paths-ignore` | Automatic |

While CI is off, the local gates are the only verifiers: the pre-push perf gate, `ctest --preset quick` every
change, and `local_ci.py --level full` before milestones. The status `local_ci.py` posts is the record of what was
verified. When CI resumes, run the full tier once with `workflow_dispatch` on each repository's `main`, to catch
anything platform-specific that accumulated meanwhile.

**Not in CI:** performance measurement (see "Performance across tiers"; deterministic metric tests do run in CI, timing does not). Shared runners are too noisy to judge it, and the local gate
(Sub0Perf `tools/perf`, quick level every push, full level at milestones) owns it. A repository may run an
informational benchmark at release.

**Private repositories** consume paid minutes, so:
- macOS (billed at 10×) and Windows (2×) run only in the full tier, which already restricts them;
- the weekly schedule must be change-gated;
- every job has a `timeout-minutes`.

## Local attestation and pull requests

`tools/ci/local_ci.py` runs a repository's declared local checks (`.ci/local.json`: the presets and commands to
run, per level) on every compiler available on the machine. On success it posts a commit status:
- **context** `local-ci/quick` or `local-ci/full`;
- **description** naming the compilers and suites, for example "GCC 16, Clang 22 x86-64-v3: ctest full 3/3";
- **target** the commit it verified.

The status is only posted for a commit that is on the remote. It records what was run; it does not replace the
smoke tier.

**Labels** (created in every repository):

| Label | Meaning |
|---|---|
| `ci:full` | Run the full tier on this pull request |
| `ci:local-verified` | The head commit carries a successful `local-ci/full` status; the full tier may be skipped for merging |
| `ci:release` | Set on release pull requests; the full tier is required |
| `ci:skip` | Run no CI tier on this pull request; `ci-policy` requires a local-ci status instead |

**Merge rule for pull requests:** smoke passes, and either the full tier passes or `ci:local-verified` is set and
the `local-ci/full` status matches the head commit. The `ci-policy` job in `cpp-smoke.yml` evaluates this when smoke finishes. The reusable `ci-policy.yml`, run as
the last job of each repository's full-tier workflow with `needs:` on all its jobs, re-evaluates it once the full tier
is done. Both report a `ci/policy` status that rulesets can require. Nothing polls: job ordering, not a waiting runner,
decides when it reports.

**Direct pushes to `main`,** the usual development flow for a single maintainer, run the smoke tier after the
push. Required status checks are therefore not enforced on `main` itself; they would block pushes. They apply to
pull requests through rulesets.

## Local gates must self-test

Local-first means the local gates are the only verifier. A gate that quietly does the wrong thing is worse than no
gate, because it reports a pass nobody earned. Two failures reached `main` on 2026-11-03:
- a performance gate measured everything on a push with nothing to push, and its quick level overran a 90 s budget to
  58 minutes on new, expensive benchmarks;
- a `.githooks/pre-push` template piped the build through `tail`. Without `pipefail` the pipeline returned `tail`'s
  status, so a failed build did not block, and the later `if [ $? -ne 0 ]` was dead code. It was also often inactive,
  because `core.hooksPath` was unset.

Rules for every pre-push gate:
1. **Skip what has nothing to check.** A push with no refs (git runs the hook with empty stdin when everything is up to
   date), a push that only deletes, and a documentation-only push exit 0 at once, without building or measuring. The
   same documentation list as the `None` tier applies.
2. **Never mask an exit status.** Use `set -euo pipefail`. A pipeline whose status matters must not end in a filter
   such as `tail`, `head` or `grep`; redirect the output to a file and show the tail when the command fails.
3. **Keep a time budget at the quick level.** Expensive or unknown work (a benchmark nobody has timed, a long suite) is
   deferred to the full level and reported as not covered; it never justifies an overrun.
4. **The hook must be active.** `git config core.hooksPath .githooks` for a committed hook; otherwise the installer
   writes into the directory `git rev-parse --git-path hooks` names. A committed hook that is not the active one is a
   finding.
5. **Prove it.** `tools/ci/hook_check.py --repo .` checks the above without a push, and prints a pass/fail table
   (non-zero exit on any failure):

   | Check | Passes when |
   |---|---|
   | active hook | A `pre-push` hook is active (`core.hooksPath` resolved, else `.git/hooks`); a committed `.githooks/pre-push` that is not the active one is reported |
   | empty push | The active hook, run with empty stdin as git runs it for an up-to-date push, in the repository with a clean environment, exits 0 within 30 s |
   | status masking | A static lint finds no pipeline whose status matters (`set -e`, a `$?` check, `||`, `if !`) that ends in a filter, unless `pipefail` is on |
   | power guard | Where a hook uses `bench_check.py`, `bench_check.py power` runs and exits 0 or 3 (["Power state"](#power-state)); the script is found in `tools/perf/`, `SUB0PERF_TOOLING_DIR`, or the newest `sub0math_tooling_dir.txt` under `build*/` and `build/*/` (the mechanism keeps its Sub0Math-era name, ADR 0004); a WARN for each power switch set in the environment |
   | tracked hooks | With `core.hooksPath` inside the worktree, no tracked file there is a `pre-push.user` or a hook generated by sub0perf (per-clone files that must stay untracked; a generated hook is recognised by the `# sub0perf hook:` line sub0perf writes) |
   | deep self-test | Every command of the `"selftest"` list in `.ci/local.json` exits 0 |

6. **Run it in local CI `quick`.** Add a step to `.ci/local.json`, so a gate that stops working is found by the next
   routine run and not by the next failure. `{ci_dir}` is the directory of the running `local_ci.py`:

   ```json
   {"name": "hook-check", "commands": ["{python} {ci_dir}/hook_check.py --repo {repo}"]}
   ```

A repository with its own gate logic declares a deep check beside the quick step, for example one that drives its gate
with synthetic pushes and a plan-only mode and measures nothing (Sub0Perf's `bench_check.py selftest`):

```json
"selftest": ["{python} tools/perf/bench_check.py selftest --repo ."]
```

`selftest` commands take the placeholders `{python}`, `{repo}` and `{ci_dir}` and run in the repository.

**Reference template:** [`templates/pre-push`](../templates/pre-push). Copy it to `.githooks/pre-push`, set its `build`
and `tests`, and activate it. It exits 0 on empty stdin and on deletions, skips documentation-only pushes, uses
`pipefail`, and keeps the build's exit status by redirecting its output to a log. `hook_check.py` passes it, and its
tests (`tools/ci/test_hook_check.py`) hold it to that.

## Power state

Timing on battery is not a fair comparison with a baseline recorded on mains: clocks, boost and the power plan all
differ, and the run burns battery for a verdict nobody can trust. A gate that measures anyway reports a regression or a
pass that means nothing. So, for every local performance gate and every wall-clock test:

1. **Never measure on battery, and never pass.** On battery or in a battery saver, a performance gate **defers** and a
   wall-clock test **skips**. Neither builds, measures or reports a pass for what it did not check, and the deferral
   exits 0 so it never blocks a push. The state is `ac`, `battery` or `unknown`, plus whether a saver is on; `unknown`
   (a desktop, a runner, a failed query) counts as mains but is reported as unknown.
2. **The message names the override.** A deferral says why and how to measure anyway
   (`on battery: performance not measured (deferred; ...). To measure anyway: set SUB0PERF_ON_BATTERY=1`).
3. **An override is loud and never becomes a baseline.** A run under the override announces it on its first line and in
   its summary, its record carries the power state, and no command that writes a baseline accepts such a record.
   The override set on mains is reported as ignored, so a stale export is noticed.
4. **Baselines are recorded on mains only.** A record or baseline carries the power state it was measured in; a
   baseline without the field is mains; a mismatch between record and baseline makes absolute verdicts inconclusive.
5. **Test the guard.** The guard has a self-test in the quick set, with the power state faked, like any other gate
   logic (above). `hook_check.py` checks that a repository using the shared perf gate has the guard.
6. **Mains is not full speed: check the sustained speed.** A mains machine can be throttled (a vendor power mode, a
   charging cap, thermal limits, or the Windows "Best power efficiency" power mode, which holds boost for only about 3 s of
   load and then falls to about 0.4x). A gate or timing test therefore compares the machine with a reference by its
   **sustained** speed, a calibration kernel run continuously for several seconds in one process, and not only by a
   burst reading of a fraction of a second, which runs inside the boost window and reads normal. A throttled machine defers
   or skips exactly like battery, the same override applies, and the message names the Windows power mode (read only,
   never changed) with its fix: set Balanced or Best performance.

Conventions, shared by every repository (they are the shared gate's names, in Sub0Perf):

| Convention | Meaning |
|---|---|
| ctest label `timing` | Marks every wall-clock test. `ctest -LE timing` is the untimed set |
| `SKIP timing: <reason>` | What a timing test prints when it does not run, with the reason and the override |
| `SUB0PERF_ON_BATTERY=1` | The override: measure on battery anyway. Flagged, and never a baseline |
| `SUB0PERF_POWER_STRICT=1` | Strict mode: a push on battery is refused instead of deferred. Off by default |
| `SUB0PERF_FAKE_POWER=ac\|battery\|unknown\|saver` | Replaces the power query. **Tests only**; set anywhere else it is a warning |
| `bench_check.py power` | Prints the state, the Windows power mode and the switches in effect. Exit 0 on mains, **3 on battery, saver or a throttled machine** (with `--exe` and `--baseline`) |

The local tools follow it:
- `tools/ci/local_ci.py` prints the power state at the start of a run. On battery it adds `-LE timing` to its plain
  `ctest` commands (a `--preset` is left alone, because a command-line filter would replace the preset's own exclusions),
  and marks the saved record and the attestation description "untimed (on battery)". Under the override it runs
  everything and marks the run "timed on battery (override)". It uses Sub0Perf's `sub0perf.power` when the repository
  has it, and a small fallback with the same states and variable names otherwise (`tools/ci/ci_power.py`); both report the
  Windows power mode (mains and battery) beside the state, and neither changes it.
- `tools/ci/hook_check.py` has a `power guard` row (a hook that uses `bench_check.py` must support `bench_check.py power`,
  exit 0 or 3) and warns for each of the three variables set in the environment. The empty-push check runs the hook
  without them.
- `templates/pre-push` needs nothing: it does not measure. A hook that calls a gate must let a deferral (exit 0) stand;
  do not turn it into a failure by treating the gate's message as an error.

## Performance across tiers

Performance is checked in two different ways, and only one of them may live in CI.

| Kind | What it checks | Where it runs |
|---|---|---|
| **Deterministic metrics** | Counts and sizes that do not depend on the machine: operations, allocations, iterations to converge, bytes, golden digests | Every tier, as ordinary ctest tests. Labelled `smoke` or `full` by cost, **never** `timing` |
| **Timing** | Wall-clock time | **Local only** (below). Never in hosted CI |

A deterministic metric is a test like any other: it fails the build when the number moves, on any runner, so it belongs in
the smoke tier when cheap and in `full` when not. A wall-clock number cannot do that on a shared runner, so no workflow in this
repository measures time, and none is to be added.

**Timing is local, in three places:**
1. **The pre-push gate.** `templates/pre-push` carries an optional, commented stanza that runs
   `python <tooling>/bench_check.py gate --refs-from-stdin` after the tests, with `SUB0PERF_OUTPUT=brief`. The gate reads the pushed
   refs, skips what has nothing to measure, and defers on battery, in a battery saver or on a throttled machine (["Power state"](#power-state)).
   **A deferral exits 0 and stands**: the hook never turns it into a failure and never sets the override.
   `<tooling>` is the Sub0Perf tooling directory the configure step recorded in `<builddir>/sub0perf_tooling_dir.txt`. A preset's
   build directory is `build/<preset>`, so the stanza uses `SUB0PERF_TOOLING_DIR` if set, else the newest record under `build*/` and
   `build/*/`; with neither it says the gate was skipped (and does not block). `hook_check.py` searches the same places.

   **Which wiring applies to a tracked hooks directory** (`core.hooksPath .githooks`, the usual case for this template). Sub0Perf's
   `install-hooks` never writes into a tracked directory, and Sub0Perf's `selftest` accepts either of two wirings of the committed
   `pre-push`:
   - *the hook runs the gate itself*: the stanza above. The template's hook reads the pushed refs from stdin (to skip
     documentation-only pushes) and hands them to the gate. **This is the wiring for this template.**
   - *the chain line* `install-hooks` prints, for a committed hook that does not read stdin itself. Do not add it to this template:
     the chained copy reads all of stdin before the template's loop, which then sees no refs.
2. **The release check.** The `full` level of `.ci/local.json` has a perf step marked `"timing": true`. `local_ci.py` skips a
   timing step with the note `untimed (on battery)` under the same power logic, so the attestation says what it did not measure. The
   step may name a `"perf_outcome"` file; `local_ci.py` folds that sub0perf outcome into the status description.
   `{sub0perf_tooling}` in its commands expands to the recorded tooling directory (the pre-rename `{sub0math_tooling}`, `sub0math_tooling_dir.txt` and `SUB0MATH_PERF_*` names are still accepted for one release):

   ```json
   {"name": "perf", "timing": true, "perf_outcome": "{builddir}/perf-outcome.json",
    "commands": [["{python}", "{sub0perf_tooling}/bench_check.py", "gate", "--level", "full",
                  "--out-json", "{builddir}/perf-outcome.json"]]}
   ```

   `--out-json PATH` (or the environment variable `SUB0PERF_OUTCOME_JSON`) makes `check`, `auto`, `gate`, `compare` and `metrics`
   write the `sub0perf-outcome-1` object to a file whatever `--format` prints, so no wrapper is needed. A run that reaches no
   verdict (a deferral, a refusal) writes `status: "NO_VERDICT"`, which `local_ci.py` shows as `perf NO_VERDICT`.
3. **A developer's own measurement** with the same tooling, on mains.

Hosted CI has **no timing**, not even an informational job. A repository that wants a release benchmark records it locally and
attaches the result.

### Test labels: tiers, areas and needs

The shared vocabulary of ctest labels, as proposed by a sibling project and meant to be lifted here (areas stay per project; tiers
and needs are shared):

| Label | Values | Meaning |
|---|---|---|
| tier (exactly one per test) | `smoke` \| `full` \| `local` | `smoke`: fast, no hardware, CI on every push. `full`: long suites, CI at tags, `ci:full` and weekly. `local`: needs this machine, never scheduled by CI |
| `area:<name>` | per project | the layer or concern covered |
| `needs:<x>` | `gpu`, `backend`, `network`, `admin`, and `quiet-machine` (below) | any `needs` forces tier `local` |
| `cost:slow` | | a long test inside its tier |

Presets follow: `quick` = not `full`, not `local` (CI smoke); `full` = `smoke` + `full` (CI full); `local` = everything.

**`timing` maps onto it as a `local`-tier test that `needs:quiet-machine`:** wall-clock time is meaningful only on mains, on a quiet
and unthrottled machine, so no hosted runner can run it, and it is never scheduled by CI. What the spec recommends:
- a repository with tiers labels a wall-clock test `local` and `needs:quiet-machine`, and **also keeps `timing`** while
  `local_ci.py` and the power conventions (["Power state"](#power-state)) key on that name (`-LE timing`, `SKIP timing:`): `timing`
  is the alias, not a fourth tier, and a test is never `smoke` or `full` and `timing` at once;
- a repository with only `smoke` and `full` (and a `timing` label, as above) is already consistent: its `full` preset excludes
  `timing`, which is the `local` tier by another name. Adding `local` costs one label and one preset;
- `needs:quiet-machine` joins the shared `needs` values; the other `needs` stay as proposed. Deterministic metric tests are ordinary
  `smoke` or `full` tests, never `local` ("Performance across tiers").

### The consumer rung

Each library is also tested as a project its consumers use: a nested project built by `ctest --build-and-test` against the installed
or in-tree package. Convention:
- the test is labelled `full` until it has been measured cheap, with a warm ccache, and then it may move to `smoke`;
- both reusable workflows export `CMAKE_C_COMPILER_LAUNCHER=ccache` and `CMAKE_CXX_COMPILER_LAUNCHER=ccache` as **environment
  variables**, not only as `-D` flags, so the nested configure inherits them and a consumer rung is cached too;
- a consumer rung measures nothing in time.

### The token-efficient output contract

Gates are run by people and by agents; an agent pays for every line it reads. So a gate:
1. prints **one verdict line** by default, plus only what failed (`SUB0PERF_OUTPUT=brief`; `local_ci.py --brief` does the same for a
   level: one line, then the failing steps and a perf step's report path and next action);
2. offers **`json`** (the sub0perf outcome, schema `sub0perf-outcome-1`: `status`, `exit`, `counts`, `changed[]`, `report`,
   `next_action`) for a program, and **`md`** for a person's report;
3. **never inlines detail.** Detail is a file; the outcome names it by `report` path and the reader opens it only when needed;
4. always gives a **`next_action`** (a command), so a failing run says what to do next.

### The improve loop

A regression or a missed target is not just reported: the Sub0Perf perf loop takes the `changed` benchmarks, the report and the
`next_action`, proposes a change, re-measures on mains and records the result. It is written up in the Sub0Perf repository
(its `docs/PERF_LOOP.md`; being written alongside this section, so look for it there by that name). This strategy fixes only the
contract the loop relies on: the outcome file above, local-only timing, and a deferral that never counts as a pass.

## Public repositories never name private ones

A public repository must not name or link a private repository, in a comment, a document, a commit message, a path or a built
artefact. `tools/ci/private_names.py` checks the staged diff (`--staged`), a pushed range (`--pre-push`, from a git hook) or
whole files (`--files`, for a built `.wasm` or archive) or the whole tracked tree at HEAD (`--tree`, a sweep for mentions committed before the guard existed) against a list of forbidden names, case-insensitively, and reports the
file and line. In a range every commit is scanned, not the net change, because a name added and then removed is still in the
published history.

The list is **local and untracked**, so the forbidden names are never published: put one name per line (`#` comments allowed) in
`~/.config/kismo/private-names.txt` (`%USERPROFILE%\.config\kismo\private-names.txt` on Windows), or point
`KISMO_PRIVATE_NAMES_FILE` at a file, or set `git config --global kismo.privateNames "name1;name2"`. Do not copy it into a repository,
a workflow or a secret. With no list the check passes and says `NOT CHECKED`, so a contributor without the list is not blocked;
`--require-list` makes that a failure. Wire it into a public repository's pre-push hook as `templates/pre-push-public-names`
does (a hook that finds the script in a sibling clone of this repository, or at `$KISMO_CI_DIR`).
Where a perf gate already owns `.git/hooks/pre-push`, install the template as `.git/hooks/pre-push.user`, which that hook runs first.
Each public repository also lists a `private-names` step (`--staged`, then `--tree`) in the `quick` level of `.ci/local.json`, so a
mention that slips past the hook is found by the next local run.

## Hygiene for every workflow

- **Pin runner images** (`ubuntu-24.04`, `windows-2022`, `macos-14`), never `-latest`, which migrates silently
  (for example `ubuntu-latest` moving to Ubuntu 26, whose package set differs).
- **Concurrency:** `group: ci-${{ github.ref }}` with `cancel-in-progress: true` on pushes and pull requests.
- **`timeout-minutes`** on every job.
- **Caches:** the CPM source cache (`CPM_SOURCE_CACHE`) and a compiler cache (ccache or sccache) keyed on the
  toolchain and the dependency pins.
- **`permissions: contents: read`** unless a job needs more.
- **Reusable workflows from this repository,** pinned to a commit SHA, so a strategy change propagates by bumping
  one ref.

## Adoption checklist

1. Replace the repository's CI workflow with callers of `cpp-smoke.yml` (push and pull request) and
   `cpp-full.yml` (tags, dispatch, the `ci:full` label, and the change-gated weekly schedule).
2. Add `paths-ignore` for documentation-only changes.
3. Label long test suites `full`, and add `quick` and `full` test presets.
4. Add `.ci/local.json`, and run `local_ci.py` before opening pull requests or at milestones.
   Add the `hook-check` step to its `quick` level ("Local gates must self-test").
   Label wall-clock tests `timing` and make them skip on battery ("Power state"); with tiers, that is `local` + `needs:quiet-machine`
   ("Test labels").
5. Create the three labels (`gh label create`).
6. Remove performance jobs from routine CI; keep timing out of hosted CI altogether ("Performance across tiers").
7. Pin runner images and set timeouts.
8. Put deterministic metrics in ctest, labelled `smoke` or `full`, never `timing`. Label a nested consumer-project test `full` until
   it is measured cheap with ccache.
9. For a repository that gates performance locally: enable the commented perf stanza in `.githooks/pre-push`, mark the release
   perf step in `.ci/local.json` `"timing": true` (with `perf_outcome`, `--out-json` and `{sub0perf_tooling}`), and run
   `hook_check.py`, which then also checks the power guard and that no `pre-push.user` or generated sub0perf hook is tracked.
10. A library built with GCC-style flags that must also build on Windows: call `cpp-full.yml` with `windows-toolchain: mingw-ucrt`
    instead of a copied MinGW job; pass `test-args: --timeout 600` where a leg needs a ctest timeout.

The reference implementations are Sub0Perf (the tooling), Sub0Math, Sub0Physics and a private bike-dynamics consumer.
