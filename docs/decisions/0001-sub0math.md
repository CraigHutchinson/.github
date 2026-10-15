# 0001: Sub0Math, shared math vocabulary with light units and named roles

- **Status:** Accepted (2026-10-15). Implemented in [Sub0Math](https://github.com/CraigHutchinson/Sub0Math)
- **Owner:** Craig Hutchinson
- **Applies to:** every Sub0 library, and to KismoTech projects through them (KismoCore, KismoEngine,
  KismoEdge, BikeDynamics, products), and Crucible

## Context

The family already has three copies of a 2D point: `sub0hexgrid::Point`, `sub0physics::Point2`, and
Filament's math types in Crucible's renderer. The next physics work (a 3D bike: lean, steer, contact
patches) needs vectors, rotations and transforms, and so do KismoEngine and Crucible. Under the
[forge rule](../forge.md), a second consumer justifies extraction; there are already three.

Two classes of bug are common in this kind of code, and the type system can remove both:

- **Unit and dimension mistakes:** millimetres passed as metres, degrees as radians, a time added to a
  distance.
- **Swapped arguments of the same type:** `makeBox(width, height, depth)` called as
  `makeBox(height, width, depth)` compiles today because all three are `double`.

Options considered: rename Sub0Physics to a broader library (rejected: an umbrella that renderers would
depend on for a vector); adopt glm or Eigen as the shared vocabulary (rejected: third-party types in
every public API, float/graphics bias or heavy templates, awkward on firmware); adopt
[mp-units](https://github.com/mpusz/mp-units) (rejected for now: complete and correct, but heavier than
we need, slower to compile, and a third-party type in every interface). "Sub0Numeric" was rejected as a
name because "numeric" suggests numerical methods (solvers, integrators), which live in Sub0Physics.

## Decision

Create **Sub0Math**: public, MIT, header-only, C++23, Sub0 style profile.

```text
sub0math/
  number/    scalar concepts, float/double policy, tolerance comparison, fixed-point
  units/     Quantity<Dimension, Rep>, units for construction and extraction, literals
  role/      Role<Tag, T>: named argument and value types
  linear/    Vec<T, N>, Mat, Quat, RigidTransform
  geometry/  Point (affine) vs Vec (displacement), Aabb, basic predicates
  interop/   optional converters to Filament, glm and Eigen; never included by the core
```

### Units: good enough, not a full unit system

```cpp
using namespace sub0math::literals;

Length height = 1.2_m;
Time dt = 500_us;
Velocity v = height / dt;            // Length / Time is a Velocity
auto wrong = height + dt;            // compile error: Length + Time
double mm = height.in(millimetres);  // units are named on the way out...
Length h2 = millimetres(350.0);      // ...and on the way in
double raw = height;                 // compile error: no implicit unwrap
```

- A quantity is a value plus a compile-time **dimension** (integer exponents of length, mass, time,
  angle and temperature). `+`, `-` and comparisons need equal dimensions; `*` and `/` combine them;
  `sqrt` needs even exponents. That is the whole algebra.
- **Values are always stored in coherent SI** (metres, kilograms, seconds, radians, kelvin). Other units
  (mm, km/h, degrees, rpm, g) are only conversion factors at construction and extraction. There are no
  mixed-scale types, no automatic rescaling between unit systems, and no affine units. This is the
  deliberate limit that keeps it small.
- **Angle is a dimension**, so `sin` and `cos` accept only angles and a degrees/radians mix-up does not
  compile.
- **Zero overhead:** `Quantity<D, double>` is the size of a `double`, trivially copyable, and
  `constexpr`. Every rule is pinned by compile-time tests, including tests that the wrong operations
  do not compile (`static_assert(!requires { Length{} + Time{}; })`).
- **Representation is a parameter, float by default.** Unqualified names and the `_m`-style literals are
  float (rendering, GPU data, firmware with single-precision FPUs, most interfaces). `sub0math::f64` holds
  double aliases and literals for simulation state, accumulated time, world positions and energy budgets
  (BikeLab's float32 solver trial reversed step-refinement trends; a float clock cannot resolve 0.5 ms
  after a few hours). Float widens to double implicitly; double narrows only explicitly. Fixed point
  follows with KismoEdge.
- **Units are exact ratios to SI** (millimetres = 1/1000, degrees = pi/180), so conversions round once:
  `millimetres(350.0)` is exactly the double nearest 0.35.
- `std::chrono` stays for wall-clock and scheduling time (Sub0Pipeline, Sub0Log); `sub0math::Time` is
  simulation time. A conversion is provided at the boundary.

### Roles: say what an argument means

```cpp
using Height = sub0math::Role<struct HeightTag, Length>;
using Width = sub0math::Role<struct WidthTag, Length>;
using Depth = sub0math::Role<struct DepthTag, Length>;

Box makeBox(Height h, Width w, Depth d);

makeBox(Height{1_m}, Width{5_m}, Depth{6_m}); // compiles
makeBox(Width{5_m}, Height{1_m}, Depth{6_m}); // compile error: arguments swapped
makeBox(1_m, 5_m, 6_m);                       // compile error: role not named
```

- A role is a one-line alias over any value type: a quantity (`Height` over `Length`), a plain number
  (`RetryCount` over `std::uint32_t`), or an id. Construction is explicit; roles never convert into each
  other; `.value()` returns the underlying value.
- Roles carry no arithmetic of their own: do maths on the underlying quantity (`h.value() + w.value()`
  is a `Length`). This keeps the facility tiny and avoids inventing what "height plus width" means.
- Roles live in Sub0Math because most of them wrap quantities; a library that only wants labels (for
  example Sub0Pipeline) depends on one header-only library.

### Where to use them

- **Public APIs** in Sub0 and KismoTech code: physical values are quantities; two or more parameters of
  the same type get roles.
- **Hot kernels** may work in raw `double`: integrator state (`sub0physics::StateVector`) stays untyped
  by design, and models convert at their edges with `.si()` / `fromSi()`. Units protect interfaces; they
  are not required inside a stage loop.
- **Records, GPU buffers, files:** unwrap explicitly at the boundary, naming the unit.

## Consequences

- Sub0Physics, Sub0HexGrid, KismoEngine, Crucible and BikeDynamics share one `Vec`/`Point`. Migration:
  `sub0physics::Point2` and `sub0hexgrid::Point` become the Sub0Math types; Sub0Physics' element and setup
  APIs move to quantities (for example `AxialSpringDamper{Force preload, Stiffness k, ...}`), and
  BikeDynamics' setups follow, with parity tests unchanged.
- Each new library or API change is reviewed against "quantities at the interface, roles for same-typed
  parameters". The Sub0 style profile gains a rule for it.
- Compile-time cost is measured on Sub0Physics before and after adoption and recorded here. If it is
  significant, the fallback is narrower use at interfaces, not dropping the checks.
- If units outgrow `sub0math/units/`, they split into a Sub0Units library without changing the API.

## Resolved questions (2026-10-15)

1. **Bare numbers in physical roles:** a unit is always required (`Height{1_m}`); `Height{1}` does not
   compile. Bare numbers remain fine for roles over plain numbers (`RetryCount{3}`).
2. **Dimensions in v1:** length, mass, time, angle and temperature. Electric current and amount of
   substance arrive with their first consumer (KismoEdge sensors).
3. **Matrix layout:** column-major, matching GPU and Filament conventions.
4. **Float or double:** float by default, double by explicit choice (above).
5. **Performance tooling** (benchmark runner, per-host budgets, VTune/Advisor/perf targets, vectorization
   reports) lives in Sub0Math, the bottom of the stack, and is shared by every library above it.
6. **Local development:** the shared CPM bootstrap is 0.43.2, which reads `CPM_<Package>_SOURCE` from the
   environment, so a developer machine builds the whole stack from local checkouts while CI builds pins.

## Follow-ups

- Primitive shapes and their purposes (bounding, collision, render meshes): see the shapes proposal.
- Station setups in BikeDynamics move to quantities and roles.
- Sub0HexGrid adopts `Point2d`, coordinated with Crucible's pin.
