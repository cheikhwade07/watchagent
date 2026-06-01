---
name: detection-replay
description: >-
  Dry-run replay of WatchAgent's CURRENT detection logic over stored readings to
  preview which events WOULD fire (without persisting anything). Use after
  changing a detector threshold/constant to check the new logic isn't too
  sensitive or too quiet — NOT for what was already detected (use data-analysis
  for that).
---

# WatchAgent detection replay (what-if / threshold tuning)

This skill re-runs WatchAgent's **detection pipeline, exactly as it is coded
right now**, over a chosen set of stored **readings** and reports which events
that logic **WOULD** produce. It is a "what-if" / dry-run tool: it answers
*"given the current thresholds, what would fire?"* — it does **not** read the
stored `events` table and it **never writes** to the database.

It runs a small CLI (`scripts/replay.py`) that:

1. opens the SAME SQLite database the app uses, **read-only**
   (`PRAGMA query_only = ON`, only `SELECT`s), resolving the path from the app's
   own config (`app.config.get_settings().db_path` / `WATCHAGENT_DB_PATH`) —
   never hardcoded;
2. loads each city's most recent readings into the real
   `app.storage.models.Reading` objects (same per-city, oldest-first slice the
   live pipeline feeds itself); and
3. runs them through the **real** detector pipeline
   (`app.detection.pipeline.run_detectors`) — the EXACT function the live poll
   cycle and the historical backfill call. The detection logic is reused, never
   reimplemented, so the replay produces the SAME events the real pipeline would
   for those readings — it's literally the live pipeline run over a chosen
   input, just not persisted.

## How it differs from the `data-analysis` skill — pick the right one

These two skills look similar but answer **opposite** questions. Choose by
whether the user asks about events that already happened or events that *would*
happen under the current code.

| If the user wants… | Use | Reads | Writes |
|---|---|---|---|
| **What WAS detected / stored** — counts, history, "how many events in Vancouver?", trends, comparisons | `data-analysis` | the `events` + `readings` tables (**did-fire**) | no |
| **What WOULD fire now** — preview the current detector logic over readings, e.g. after editing a threshold | `detection-replay` (this skill) | re-runs detectors over `readings` in memory (**would-fire**) | no |

In short: **`data-analysis` = did-fire** (queries what was stored);
**`detection-replay` = would-fire** (re-runs the detectors and shows the result
without saving it).

## When to use this skill

- After **changing a threshold or constant** in `app/detection/detectors.py`
  (e.g. `TEMP_MAD_MINOR`, `PRECIP_ONSET_MM`, `FRONTAL_TEMP_DROP_C`) and **before
  committing**, to preview the impact: does the new value make detection too
  sensitive (a flood of events) or too quiet (nothing fires)?
- To sanity-check that a refactor of the detectors still produces the events you
  expect over real stored readings.
- Any time you want to see "what would the current logic flag?" without mutating
  the stored event history.

If instead you want to know what the system has *already* detected and saved,
use the `data-analysis` skill.

## How to use

Run the script and read the JSON it prints to stdout:

```bash
python .cursor/skills/detection-replay/scripts/replay.py [options]
```

It prints a single JSON object with the same envelope as the `data-analysis`
skill:

```json
{
  "query": "<what was asked>",
  "result": { "...structured data..." },
  "summary": "<one-line plain-language answer>"
}
```

Read `summary` for a quick readout; read `result` for the structured numbers.

### Options

| Option | Meaning | Default |
|---|---|---|
| `--city CITY` | Restrict the replay to a single city. | all cities |
| `--last N` | Replay only the most recent `N` readings per city (bounded & deterministic). | 200 |

### Example invocations

```bash
# Replay all cities over the last 200 readings each (the default).
python .cursor/skills/detection-replay/scripts/replay.py

# Preview just one city.
python .cursor/skills/detection-replay/scripts/replay.py --city Vancouver

# Tighter, faster window after tweaking a threshold.
python .cursor/skills/detection-replay/scripts/replay.py --last 72

# One city, custom window.
python .cursor/skills/detection-replay/scripts/replay.py --city Ottawa --last 100
```

### What `result` contains

- `last_n` — the per-city replay bound that was used.
- `total_events` and `by_type` — totals across every replayed city.
- `cities` — per city:
  - `readings_replayed` — how many readings were fed in;
  - `events` — how many events the replay produced;
  - `by_type` — counts broken down by `event_type`;
  - `by_severity` — counts broken down by `severity` (`unrated` for the
    `frontal_passage` / `apparent_gap` detectors, which are not graded);
  - `replayed_events` — the list of events, each with `city`, `event_type`,
    `severity`, `started_at`, `ended_at`, and the plain-language `reason`.

The `summary` line reads e.g.
`Replay over last 200 readings/city produced 5 events (3 temp_anomaly, 2 precipitation).`

## Notes

- **Read-only and non-persisting.** The connection is opened with
  `PRAGMA query_only = ON`, and the detected events are only printed — the
  `events` table is never touched. Re-running this never changes stored data.
- If the database is empty or missing, the script still returns the normal JSON
  envelope with an empty/zero `result` and a `summary` of
  "No data available — run the backfill or let the poller collect readings." It
  never crashes with a traceback.
- A bad argument (e.g. `--last 0`) prints a JSON error envelope (with an `error`
  field) and exits non-zero — never a stack trace.
- To populate readings first: `python -m app.backfill` (one-shot historical
  import) or let the live poller collect readings.
