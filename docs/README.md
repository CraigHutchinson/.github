# Docs

| Doc | What it covers |
|---|---|
| [sub0-family.md](sub0-family.md) | The Sub0 libraries: purpose, licence, consumers, status |
| [forge.md](forge.md) | How libraries are born, proven and extracted (the forge rule) |
| [users.md](users.md) | Who uses Sub0: a usage diagram and the list of projects |
| [decisions/](decisions/README.md) | Cross-repository decisions (ADRs) |
| [ci-strategy.md](ci-strategy.md) | CI gold standard: local-first, a smoke tier per change, the full matrix at releases; reusable workflows, labels and local attestation |

Dependencies point one way: projects use Sub0, and Sub0 never depends on them. Users' own decisions live
in their repositories (for example [KismoTech/.github](https://github.com/KismoTech/.github/tree/main/docs)).
