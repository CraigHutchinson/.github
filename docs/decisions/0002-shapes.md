# 0002: Shapes in Sub0Math, grown by use

- **Status:** Accepted (2026-10-15); the API is refined as consumers adopt it
- **Owner:** Craig Hutchinson
- **Builds on:** [ADR 0001](0001-sub0math.md)

## Context

Physics, games and rendering all need primitive shapes (spheres, boxes, capsules, planes, segments,
meshes) and need them in different roles: a bounding volume for culling, a collision shape with a
material, a mesh to draw. Designing the whole catalogue up front would guess at APIs no consumer has
used yet.

## Decision

1. **Shapes are plain values in Sub0Math** (`geometry/`), generic over the coordinate type (`f64::Length`
   for physics, `float` for rendering), defined in their own frame. Queries are free functions
   (`closestPoint`, `contains`, `boundingBox`, later `distance` and `intersects`), `constexpr`, never
   allocating, and benchmarked against hand-written code.
2. **Grow by use.** A shape or query is added when a consumer needs it, adopted by that consumer in the
   same change, and its API refined by using it. The first slice is what Sub0Physics already used:
   `Sphere<T, N>` (`Circle<T>`), `Segment<T, N>` with `closestPoint`, and `boundingBox` for both, next to
   the existing `Aabb`. Its terrain clearance query and BikeDynamics' terrain probe now take a `Circle`.
3. **Conversions are functions, not types.** A shape becomes triangles through `toMesh(shape, ...)`.
4. **Placement is separate from shape.** A shape does not store its pose; a body's pose places it. A
   `Posed<S>` pairing is added when the first consumer needs one.
5. **Purposes carry their own data and live with their owner.** Bounding volumes are pure geometry
   (Sub0Math). Collision shapes carry contact materials, margins and filters (Sub0Physics). Render
   meshes carry materials and GPU resources (KismoEngine). Each purpose is a distinct type, so a culling
   sphere cannot be passed to a contact solver by mistake.
6. **Meshes:** non-owning views in Sub0Math when first needed; owning meshes in the consumer that builds
   them.
7. **Performance:** small hot functions use `SUB0MATH_FORCE_INLINE`. Adopting `closestPoint` pushed a
   Sub0Physics helper past GCC's inlining budget (+60% on clearance) until it did.

## Added since

| Date | Shapes | Consumer and evidence |
|---|---|---|
| 2026-10-16 | `OrientedBox` (with `AabbOverlapTester`), `Capsule`, `Plane`, `Triangle` (nearest point reports the vertex, edge or face), `Ray`, `RegularGrid` | The terrain plan's groundwork (steps G1–G3: pages, corridor coverage, exact height queries, support-normal rays), whose needs were measured first by the D0 experiment. They were added as one batch ahead of those consumers, an exception to rule 2 justified by the measured plan. Paired benchmarks: triangle and ray at parity with hand-written kernels. Box-against-blocks has a recorded 1.1× gap and follow-up |

Rule added with them: the hand-written side of a paired benchmark is the fastest known code and is
never weakened. A gap is closed by evolving the API, or recorded as an open gap with a follow-up
(Sub0Math `docs/PERFORMANCE.md`). Iterated code carries a short `// PERF:` note.

## Next candidates, each with its trigger

| Shape or type | Trigger |
|---|---|
| `Posed<S>`, `Bounding<S>` | First broad phase (multiple bodies or terrain tiles) |
| `Collision<S>` with materials | First contact solver work in Sub0Physics |
| Mesh views, `toMesh` | Scanned or generated trail terrain; first render consumer |
| Torus or swept-disc tyre envelope | R1 distributed tyre contact, if the capsule envelope proves too coarse |
| `InFrame<Frame, V>` frame tags | The terrain API boundary (G1) |
