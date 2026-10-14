# Ecosystem: Sub0, Crucible and the Kismo platform

```text
 KismoTech products (closed)        Crucible (public, AGPL-3.0)       other projects
            │                                │
            ▼                                ▼ (later: proving consumer)
   KismoEdge + KismoBspNrf54 (closed)   KismoEngine (public, AGPL-3.0 or commercial)
            └──────────────┬─────────────────┘
                     KismoCore (public, MIT)
                           │
                    Sub0 libraries (this account)
```

- **Sub0** is the generic layer: it knows nothing about Kismo.
- **Kismo** composes Sub0 into a shared architecture for devices, apps, simulation and games. Its
  decisions and repository map live in [KismoTech/.github](https://github.com/KismoTech/.github/tree/main/docs).
- **Crucible** stays the Sub0 forge. It is also KismoEngine's proving consumer: KismoEngine's loop will be
  seeded from Crucible's proven, product-neutral runtime pieces.
- **Dependency direction:** closed may depend on open, never the reverse; lower layers never depend on
  higher ones.
- **Ownership:** all copyright is held by Craig Hutchinson. `KismoTech` is a GitHub organisation that
  groups product repositories.
