# 0003: Own the dynamics core in Sub0Physics; MuJoCo as a reference, not the runtime

- **Status:** Proposed (2026-10-19), for decision
- **Owner:** Craig Hutchinson
- **Builds on:** [ADR 0001](0001-sub0math.md), [ADR 0002](0002-shapes.md); BikeDynamics' terrain design
  (private) and KismoEngine's runtime goals

## The question

BikeLab's connected bicycle work runs on MuJoCo 3.16.0 (WASM). It has proven three experiments there:

- a moving deformable wheel;
- the coupled hardtail/full-suspension rig;
- the trail bike with rider and faceted impacts.

The C++ port can either:

- adopt MuJoCo as the product runtime;
- build a dynamics core of our own in Sub0Physics;
- or combine the two.

The choice is hard to reverse later, because models, contact laws, replay archives and terrain
integration all grow around the engine.

## What the problem actually needs

These requirements come from BikeLab's delivery plan (D1–D12), the terrain design, KismoEngine's
goals (deterministic fixed step, replay with full-state parity, game, headless batch, WASM and
Python front ends) and our own rules. Each one decides something below.

| # | Requirement | Source |
|---|---|---|
| N1 | **Constitutive contact we define:** distributed deformable tyre, layered soil with in-step deformation, compaction and hardening, granular (sand and gravel) ploughing, sharp edges and faceted rocks, all through our terrain extracts and support frames | Terrain design §6–7; D1, D4, D6–D8 |
| N2 | **Exact accounting:** forces, impulses, work and reactions at every interface, including component loads for failure criteria | BikeLab common acceptance rules; D10 |
| N3 | **Strict limits:** travel stops and buffers that are never overshot without being reported | BikeLab overload contract; D9 |
| N4 | **Stiff elements without tiny steps:** tyre casing, soil and buffers integrated implicitly with their own Jacobians | Real-time budget (D12) |
| N5 | **Fidelity tiers, state transfer, multirate subcycling, adaptive wheel mesh** (non-uniform, topology changes) | PHYSICS_FIDELITY; ADAPTIVE_WHEEL; terrain §7 |
| N6 | **Determinism and replay:** bitwise within a build; across platforms (native and WASM) where KismoEngine needs parity; archives that survive engine upgrades | KismoEngine; terrain §9.2 |
| N7 | **Performance:** float where valid, allocation-free stepping, SIMD-friendly data, measured against hand-written references | ADR 0001; perf tooling rules |
| N8 | **3D destination:** steering, lean, camber, an articulated rider with hand, foot and saddle contacts, multi-link rear suspensions (closed loops) | D5, D6, D9 |
| N9 | **Integration:** units and typed vocabulary at boundaries, ECS-friendly state, our paged terrain service, C++23 | ADR 0001; KismoEngine |

The size of the problem matters as much as its kind. One bike plus rider is roughly 10–40 degrees of
freedom in a tree, with a few loop closures and a handful of rider contacts. Almost all contact force
is at two tyres, and our own code computes it from our own terrain. This is **not** general robotics
with arbitrary body-to-body contact, which is where a general engine earns its keep.

## MuJoCo against those needs

Two sources were used:

- [MuJoCo documentation](https://mujoco.readthedocs.io/en/stable/), read 2026-10-19;
- BikeLab's recorded experience with the pinned 3.16.0 source, in its BIKE_RIG, MOVING_WHEEL,
  TRAIL_BIKE, ADAPTIVE_WHEEL, FORK_BUFFERS and ITERATION_12/15/16 notes.

**Strengths, which are real:**

- mature reduced-coordinate articulated dynamics with implicit integrators;
- loop closures through equality constraints;
- deformable "flex" bodies;
- a robust convex constraint solver;
- analytic smooth derivatives;
- runtime model editing that preserves state (`mjSpec`, `mj_recompile`);
- official WASM, Apache-2.0, very widely used;
- BikeLab already has working, evidenced models on it.

**Conflicts with our needs:**

| Need | Finding | Consequence |
|---|---|---|
| N1 | The contact force law is fixed: soft, regularised convex optimisation. "This model is inherently soft"; penetration is intrinsic; the hard limit R → 0 "is not allowed". Callbacks and plugins customise geometry (`mjCOLLISIONFUNC`, SDF plugins), filtering and parameters, not the law | Our tyre and soil laws must sit **outside** MuJoCo's contact solver, as applied or passive forces. The engine would carry the bike tree; the physics we care most about would be ours anyway |
| N4 | The implicit integrator differentiates "all forces except the constraint forces", but no documented route gives plugin or callback forces derivatives. BikeLab found custom applied force "excluded from the native implicit derivative" | Tyre and soil forces are explicit and held for the step (BikeLab: first-order coupling, 31 µs steps for the ring wheel). That is the cost we most need to avoid |
| N2 | Post-constraint RNE omits flex contacts; reactions need a separate diagnostic solve plus manual flex corrections (BikeLab iterations 12 and 16). Native energy excludes custom potentials | Observability is work around the engine, not designed in |
| N3 | All limits are soft constraints; strict travel needs an external accepted-state guard | Workable; BikeLab does this, but every limit carries a guard |
| N5 | Flex edge stiffness is per flex, not per edge, which blocks the non-uniform adaptive wheel (BikeLab ADAPTIVE_WHEEL). Model edits go through recompilation | Tiering and adaptive meshes fight the model format |
| N6 | "Exact reproducibility is only guaranteed within a single version, on the same architecture"; trajectories differ between versions and operating systems | Replay archives and goldens are tied to one MuJoCo build. Native-to-WASM parity is not bitwise. Upgrades churn every baseline |
| N7 | Only double precision is distributed (`mjUSESINGLE` exists but is unsupported). Data layout, allocation and threading (islands; flex falls back to a monolithic solve under `discrete`) are the engine's | Little room to apply our float, SoA and benchmark-parity rules to the hot path |
| Terrain | Meshes collide as convex hulls; height fields cap at `mjMAXCONPAIR` = 50 contacts per pair and must be world-attached; the broadphase "cannot be modified" | Our paged B+E+F+K terrain needs custom collision functions or SDF plugins (gradient descent), or bypassing collision entirely, which is N1 again |
| Stability | Callbacks are "global and not model-specific" and "generally deprecated" for extension; the plugin conventions "may change"; no ABI promise; numerics change between releases | Our extension points would rest on the least stable parts of its API |

**Net assessment.** For this problem MuJoCo would supply the rigid multibody tree and soft limits.
Everything that makes the bike *ours* would live outside it, in the form that hurts most:

- explicit forces coupled at first order;
- reactions reconstructed after the fact;
- determinism bounded by the engine's version and architecture.

That is the vendor lock-in the question anticipated. It sits less in the API calls than in the shape
our physics would be forced into.

## Other engines, briefly

These are from general knowledge; verify before relying on them.

- **Project Chrono:** broad multiphysics with tyres and SCM terrain, and C++ native. Its terrain and
  tyre families are its own (BikeLab's research found SCM's vertical grid unsuitable for layered
  roots), there is no established WASM path, and it is heavy. It fits as an offline reference for
  specific mechanisms, not as the runtime.
- **Drake:** excellent multibody and hydroelastic contact, research-grade, and heavy. Same reasoning
  as Chrono.
- **PhysX and Bullet (articulations):** game-grade reduced coordinates, but contact laws are
  likewise fixed, and determinism and WASM vary.
- **Jolt:** a modern game physics engine with a cross-platform deterministic mode, built on maximal
  coordinates and rigid bodies. A strong candidate for KismoEngine's **general world** (props,
  debris, non-bike bodies), not for the bike's constitutive core.

None of them lets us define the contact law, couple it implicitly, and guarantee our determinism.

## What a Sub0Physics core would be

The scope is bounded, and that is what makes owning it realistic:

| Component | Scope | Notes |
|---|---|---|
| Multibody tree | Reduced coordinates; revolute, prismatic, planar and free joints; mass-matrix factorisation (CRBA, or ABA for larger trees); quaternion orientation | 10–40 DOF: dense factorisation is trivial in cost. Sub0Math already has `Quat` and `RigidTransform` |
| Force elements | Springs, dampers, buffers, tyre ring, soil and contact, each a typed element supplying force **and Jacobians** | Implicit coupling of stiff elements: the main win over MuJoCo (N4) |
| Constraints | Loop closures (multi-link rears), strict unilateral limits, rider attachments | Few constraints: a small direct solve or a projected method, with exact reaction output (N2, N3) |
| Integrators | Implicit (linearised backward Euler, then higher order where measured); the existing RK family for non-stiff parts; event location | Already partly in Sub0Physics (`ExplicitRungeKutta`, `stepTick`, `FidelitySelector`) |
| Contact | Our tyre and soil laws over terrain extracts; frame and pedal strikes against E features; working set | Designed in the terrain plan; no engine supplies it |
| Determinism | Fixed operation order, no FMA contraction in the core, our own transcendental functions where results cross platforms | Designable from the start (N6) |
| Observability | Reactions, work and energy ledger per element by construction | `EnergyLedger` already exists |

What we give up:

- MuJoCo's maturity on general contact-rich robotics, which we don't need;
- a ready flex substrate (our ring wheel becomes our own element);
- BikeLab's direct engine-level parity for D2 and D3.

What we take on: a real engineering effort, with risk. That risk is controlled below.

## Decision (proposed)

1. **Sub0Physics owns the dynamics core.** It is a reduced-coordinate multibody tree with typed force
   elements that supply Jacobians, a small constraint layer, implicit integration, deterministic
   stepping and built-in reaction and work accounting. BikeDynamics builds the bike, wheel, rider and
   contact models on it; the product runtime links no third-party dynamics engine.
2. **The model description is ours.** Bodies, joints, elements, limits and contact sites are
   described in Sub0Physics types (units, roles), not in MJCF. That description is the asset that
   must not be locked in.
3. **MuJoCo becomes a reference backend, not a dependency of the product.** A test-only adapter
   compiles the same model description to `mjSpec` for native MuJoCo 3.16.0, the version BikeLab
   pins. That gives:
   - **BikeLab parity** for the MuJoCo-based experiments: run BikeLab's MJCF natively and compare
     with its WASM goldens;
   - **cross-checks of our core** wherever MuJoCo is the stronger authority: rigid trees, loop
     closures, soft-limit regimes;
   - the **shadow reference** that the physics-fidelity contract asks for.

   It lives in a separate target, never in the shipped runtime.
4. **Contact parity is by model, not by engine.** Where BikeLab's results depend on MuJoCo's soft
   contact (`solref`/`solimp`), our core implements an equivalent regularised contact law as one
   selectable law. Parity is judged within declared tolerances against the reference adapter, not
   bitwise. Our own tyre and soil laws are separate laws, compared with their own references.
5. **Engine-free BikeLab ports continue unaffected:** linkage, geometry catalogue, buffers, terrain
   request (R0a), static footprint, wheel limits.
6. **Jolt (or similar) may later serve KismoEngine's general world**, behind KismoEngine's boundary.
   That is a separate decision.

## Delivery with gates, so the risk stays bounded

| Step | Delivers | Gate |
|---|---|---|
| M0 | MuJoCo 3.16.0 reference adapter (test-only target) running BikeLab's bike-rig MJCF natively | Matches BikeLab's WASM goldens within declared tolerances (same version; not bitwise) |
| M1 | Planar multibody core: tree, joints, CRBA, implicit integration of force elements with Jacobians, strict limits, reactions, energy ledger | Reproduces the bike rig (hardtail and full suspension, buffers, rider guide) against M0 and analytic references; allocation-free; benchmarked |
| M2 | Ring-wheel element and the regularised soft-contact law; then our distributed tyre law over terrain extracts | Moving-wheel scenarios against M0 (soft law) and against momentum, work and impulse references (our law); timestep advantage measured |
| M3 | Connected trail bike (two wheels, frame, suspension, rider) on our core | BikeLab trail-bike parity via M0 within tolerances; a determinism hash identical across native platforms and WASM |
| M4 | 3D: steering, lean, camber; loop-closure rears; articulated rider | BikeLab D5/D6 when they land |

**Fallback, decided now.** If M1 or M2 misses its gates within a bounded effort (to be set: suggested
four focused iterations), the same model description compiles to MuJoCo as the runtime instead. Since
the description and the force-element contracts are ours, falling back costs an adapter, not a
rewrite. Our tyre and soil laws keep their own form either way, because MuJoCo could not host them
natively.

## Consequences

- One more major component to own and verify. The verification machinery already exists: goldens,
  analytic references, energy ledgers, convergence tests, the perf gate, and now the MuJoCo reference.
- BikeLab can keep MuJoCo in the browser lab. The lab proves models; the product owns their
  implementation. Parity tooling bridges the two.
- Licences stay clean: no runtime engine dependency in proprietary BikeDynamics or AGPL/commercial
  KismoEngine. MuJoCo (Apache-2.0) is used only in test tooling.
