---
name: data-analysis
description: >-
  Answer questions about WatchAgent's stored weather data by running a
  read-only Python analysis CLI over the app's SQLite database. Use when the
  user asks how many events occurred, what happened in a city, temperature
  trends over time, a side-by-side comparison of the cities right now, or why
  frontal-passage events rarely/never fire.
---

# WatchAgent data analysis

This skill answers questions about the weather **readings** and detected
**events** that WatchAgent has stored in its SQLite database. It runs a small
CLI (`scripts/analyze.py`) that queries the SAME database the app writes to.

The script is **read-only**: it opens the database with `PRAGMA query_only = ON`
and issues only `SELECT` statements, so running it can never modify stored data.
It resolves the database path from the app's own config
(`app.config.get_settings().db_path` / the `WATCHAGENT_DB_PATH` environment
variable) — never hardcoded.

## How to use

Run the script and read the JSON it prints to stdout:

```bash
python .cursor/skills/data-analysis/scripts/analyze.py <subcommand> [options]
```

Every subcommand prints a single JSON object with the same envelope:

```json
{
  "query": "<what was asked>",
  "result": { "...structured data..." },
  "summary": "<one-line plain-language answer>"
}
```

Read `summary` for a quick answer; read `result` for the structured numbers.

## Which subcommand to use

| The user is asking… | Use | Example phrasings |
|---|---|---|
| How many events / what happened, where? Counts by type or severity. | `summary` | "How many events in Vancouver?", "What's been detected so far?" |
| Temperature trend / over time / recent min-max-mean. | `trends` | "What's Ottawa's temperature trend?", "Last 48h temps?" |
| Compare the cities right now / who's warmest. | `compare` | "Compare the three cities", "Which city is warmest now?" |
| Why are frontal events rare / did any nearly fire? | `near-miss` | "Why no frontal_passage events?", "Any near-misses?" |

## Subcommands and example invocations

### `summary` — counts of events and readings
Per-city event counts grouped by `event_type` and by `severity`, plus total
readings and total events. Optional `--city` scopes to one city.

```bash
python .cursor/skills/data-analysis/scripts/analyze.py summary
python .cursor/skills/data-analysis/scripts/analyze.py summary --city Vancouver
```

### `trends` — temperature statistics over a window
Per-city temperature min/max/mean/median over the last `--hours` hours
(default 72). The window is anchored to each city's most recent reading, so it
works for historical/backfilled data too.

```bash
python .cursor/skills/data-analysis/scripts/analyze.py trends
python .cursor/skills/data-analysis/scripts/analyze.py trends --city Ottawa --hours 48
```

### `compare` — cross-city snapshot
Most recent reading per city plus a recent temperature mean, so the cities can
be compared side by side (including warmest/coolest right now).

```bash
python .cursor/skills/data-analysis/scripts/analyze.py compare
```

### `near-miss` — frontal-passage diagnostic
Scans each city's chronological readings over rolling `FRONTAL_WINDOW` windows
and reports windows where exactly 2 of the 3 frontal signals (temp drop, wind
spike, precip onset) crossed their thresholds but not all 3 — i.e. it nearly
fired a `frontal_passage` but didn't. Reuses the real detector constants from
`app/detection`. Use this to investigate whether the compound frontal threshold
is too strict. Optional `--city` scopes to one city.

```bash
python .cursor/skills/data-analysis/scripts/analyze.py near-miss
python .cursor/skills/data-analysis/scripts/analyze.py near-miss --city Vancouver
```

## Notes

- If the database is empty or missing, every subcommand still returns the normal
  JSON envelope with an empty/zero `result` and a `summary` of
  "No data available — run the backfill or let the poller collect readings." It
  never crashes with a traceback.
- A bad subcommand or bad argument prints a JSON error envelope (with an `error`
  field) and exits non-zero — never a stack trace.
- To populate data first: `python -m app.backfill` (one-shot historical import)
  or let the live poller collect readings.
