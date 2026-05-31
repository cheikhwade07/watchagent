"""In-process poll loop.

Periodically calls the Open-Meteo client, persists readings via the storage
layer, and feeds them into the detection pipeline. Exposes run_poller() as
the entry point started from the app lifespan.

Phase 3 implements this.
"""

# Phase 3: implement async run_poller() driving the poll interval loop.
