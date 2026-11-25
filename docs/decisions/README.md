# Decisions

Cross-repository decisions for the Sub0 family. Newest last.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-sub0math.md) | Sub0Math: shared math vocabulary with light units and named roles | Accepted |
| [0002](0002-shapes.md) | Shapes in Sub0Math, grown by use; purposes live with their owner | Accepted |
| [0003](0003-dynamics-engine.md) | Own the dynamics core in Sub0Physics; MuJoCo as a test-only reference, not the runtime | Accepted |
| [0004](0004-sub0perf.md) | Sub0Perf: the performance tooling moves out of Sub0Math into its own repository above Sub0Platform | Accepted |
| [0005](0005-machine-scheduling.md) | Machine-wide scheduling: one per-user lease (Sub0Platform mechanism, Sub0Perf policy), shared and exclusive modes, wait-then-defer, a blocked report | Accepted |
