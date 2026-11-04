# CraigHutchinson/.github

Cross-repository documentation for the projects on this account: the **Sub0** C++ library family,
and **Crucible**, the game that forges them.

Start at [docs/README.md](docs/README.md).

> This repository deliberately holds no community health files (CONTRIBUTING, SECURITY, …).
> GitHub would apply them as defaults to every repository on the account that has none of its own.

## CI for the family

The reusable workflows in `.github/workflows` and the local attestation tool implement
[docs/ci-strategy.md](docs/ci-strategy.md):

| File | Purpose |
|---|---|
| `cpp-smoke.yml` | Everyday tier: configure, build with warnings as errors, `ctest --preset quick`; plus the `ci-policy` job (`ci/policy` status) |
| `cpp-full.yml` | Release-complete tier: Linux GCC/Clang, optional FMA leg, MSVC, optional macOS, optional ASan+UBSan, `ctest --preset full` |
| `change-gate.yml` | Decides whether the full tier runs: `CI_MODE`, `ci:skip`, `ci:full`, and the weekly change gate |
| `tools/ci/local_ci.py` | Runs a repository's `.ci/local.json` checks locally and posts a `local-ci/<level>` commit status (`python -m unittest discover -s tools/ci` runs its tests) |
| `tools/ci/hook_check.py` | Proves a repository's local pre-push gate works (active, quick on an empty push, no masked exit status, its own `selftest`); `templates/pre-push` is the corrected reference hook |
| `tools/ci/private_names.py` | Fails a push (or a staged change, or a built file) that names a private repository; the forbidden names come from a local untracked list, never from the repository (see ci-strategy.md) |

Callers pin these workflows by commit SHA of this repository.
