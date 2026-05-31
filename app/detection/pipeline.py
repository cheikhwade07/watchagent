"""Detection pipeline.

Runs the configured detectors over new readings, applies debounce/state
tracking to avoid duplicate alerts, and persists confirmed events.

Phase 4 implements this.
"""

# Phase 4: implement pipeline orchestration with per-location state model.
