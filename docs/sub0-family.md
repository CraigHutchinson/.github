# The Sub0 family

Product-agnostic C++ libraries (mostly C++23), each independently buildable and tested, consumed at
exact pins (CPM). Copyright Craig Hutchinson.

| Library | Purpose | Licence | Known consumers |
|---|---|---|---|
| [Sub0Pub](https://github.com/CraigHutchinson/Sub0Pub) | Typed publish-subscribe; compiles to direct calls when receivers are known | MIT | Crucible, Pouncer |
| [Sub0Log](https://github.com/CraigHutchinson/Sub0Log) | Compact, bounded logging and diagnostics | MIT | Crucible, Pouncer |
| [Sub0Pipeline](https://github.com/CraigHutchinson/Sub0Pipeline) | DAG job scheduler with explicit dependencies and executor model | MIT | Crucible, Pouncer |
| [Sub0ECS](https://github.com/CraigHutchinson/Sub0ECS) | Entity Component System with storage laid out by use case | AGPL-3.0-only or commercial | Crucible; KismoEngine (planned) |
| [Sub0HexGrid](https://github.com/CraigHutchinson/Sub0HexGrid) | Hexagonal topology and geometry | **none yet** | Crucible |
| [Sub0MemPage](https://github.com/CraigHutchinson/Sub0MemPage) | Paging orchestration over memory-mapped regions | MIT | Sub0Llm (design) |
| [Sub0TieredCache](https://github.com/CraigHutchinson/Sub0TieredCache) | Tiered cache / residency | MIT | Sub0Llm |
| [Sub0Reflect](https://github.com/CraigHutchinson/Sub0Reflect) | Parser-less compile-time reflection (C++17) | MIT | — |
| [Sub0Llm](https://github.com/CraigHutchinson/Sub0Llm) | LLM runtime experiments and storage stack | **none yet** | — |
| [Sub0h264](https://github.com/CraigHutchinson/Sub0h264) | H.264 decoding (see its patent notice) | MIT | — |
| [Sub0Physics](https://github.com/CraigHutchinson/Sub0Physics) | Physics primitives and their verification: transactional RK4/midpoint integrators, spring/damper and contact elements, closed-form references, energy/error/convergence checks, terrain clearance, fidelity (LOD) selection, ITT profiling markers; typed interfaces on Sub0Math; no domain models. First primitives ported from [BikeLab](https://github.com/CraigHutchinson/BikeLab) | MIT | BikeDynamics (KismoTech) |
| [Sub0Math](https://github.com/CraigHutchinson/Sub0Math) | Shared math vocabulary: vectors, points, matrices, quaternions, rigid transforms, dimensioned quantities (float by default, `f64` by choice) and named argument roles; hosts the family performance tooling. [ADR 0001](decisions/0001-sub0math.md) | MIT | Sub0Physics, BikeDynamics; KismoEngine, Crucible, Sub0HexGrid (planned) |
| Sub0Dsp *(planned)* | FFT, windows, filters, fixed-point, feature building blocks | MIT (planned) | Kismo products (first DSP need) |

## Known gaps

- **Sub0HexGrid** and **Sub0Llm** have no licence file, so they are all rights reserved by default.
- **Sub0ECS**'s `LICENSE` starts with a dual-licensing header, so GitHub doesn't detect AGPL-3.0. Consider
  moving the header to `NOTICE` and keeping `LICENSE` as the verbatim AGPL text (as Crucible and
  KismoEngine do).

Tracked in this repository's issues.
