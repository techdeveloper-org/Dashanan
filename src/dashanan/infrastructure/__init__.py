"""Infrastructure layer: adapters implementing domain ports.

This is the only layer permitted to know about concrete mechanisms
(the event bus, the system clock, storage). Per HLD Section 3.0,
dependencies point inward: this package imports `dashanan.domain`,
never the other way around.
"""
