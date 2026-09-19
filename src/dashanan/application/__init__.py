"""Application layer: use cases orchestrating the domain (HLD Section 3.0).

Depends on `dashanan.domain` only. Composition of concrete infrastructure
adapters (which `ZoneRepository`, `EventBus`, `Clock` implementation to
bind) happens at the caller's composition root, not inside this package.
"""
