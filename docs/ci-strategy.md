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
- the performance gate (`Sub0Math` tooling);
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

**Not in CI:** performance measurement. Shared runners are too noisy to judge it, and the local gate
(`Sub0Math` `tools/perf`, quick level every push, full level at milestones) owns it. A repository may run an
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
5. Create the three labels (`gh label create`).
6. Remove performance jobs from routine CI.
7. Pin runner images and set timeouts.

The reference implementations are Sub0Math, Sub0Physics and KismoTech/BikeDynamics.
