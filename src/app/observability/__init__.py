"""Events, metrics, tracing.

Ports and vocabulary, no sinks. Scope override item 8 defers the lot: the demo creates neither
`job_events` nor a metrics table (D093), so the adapters here drop what they are given and say
so. D054 still stands for whoever builds them -- observation is read out of SQL, not out of a
second system.

Trace id creation is the exception and is real; see `tracing.py`.

Imported by `main.py` only. Nothing in `domain/` knows this package exists.
"""
