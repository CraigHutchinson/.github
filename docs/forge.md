# The forge rule

Libraries here are **born inside real consumers**, not designed in isolation. Crucible is the main
forge: a demanding game whose [reuse catalog](https://github.com/CraigHutchinson/Crucible/blob/main/docs/reuse/README.md)
records every candidate, upstream finding and extraction.

For each candidate capability, pick one disposition:

1. **Retain local:** product policy, or only one consumer.
2. **Reuse** an existing library.
3. **Improve upstream:** fix or extend the owning library from an exact-version reproduction and fixture.
4. **Extract:** only when a **second consumer** needs it and it has tests. Extract with history; leave one
   source of truth, never two copies.

Rules:

- **Product-neutral APIs:** domain names and policy stay in the product.
- **Pins, not HEADs:** consumers pin full SHAs or versions, and change pins deliberately.
- **Independent gates:** a library builds and tests on its own, with consumer validation recorded separately.
- **No umbrella framework:** compose small libraries in the consumer.

The same rule governs the [Kismo platform](ecosystem.md): KismoCore and KismoEngine are extracted from
proven code in Pouncer, PhantomFlow and Crucible.
