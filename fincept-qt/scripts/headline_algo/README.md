# headline_algo — phase one (paper only)

A headline-driven trading pipeline that runs beside Fincept Terminal and talks to it the
same way an external AI agent does: through the terminal's built-in MCP server. It never
touches a broker. Every order goes to the terminal's paper engine, and every decision,
taken or skipped, is written to a local SQLite journal and summarised into a Note once a
day.

## What it does each cycle

1. **Ingest** news clusters (multi-source stories with velocity and a breaking flag) and
   HIGH/CRITICAL threat alerts from the terminal. The credit-metered geopolitics feed is
   off unless `use_geopolitics_feed` is set.
2. **Extract** one fixed-schema event per story: type (earnings miss, guidance raise,
   sanctions, central-bank hawkish, outage, conflict escalation, …), direction,
   confidence, companies, tickers, macro targets.
3. **Map** the event to US-listed instruments: tagged tickers, company names resolved via
   the terminal's symbol lookup, and macro themes turned into ETFs via `macro_map.json`
   (with per-event overrides, e.g. conflict escalation is bullish for defense and gold).
   Every candidate must return a live quote or it is dropped.
4. **Decide** with the rule layer: tradeable event type, confidence floor, long-only,
   no duplicate position, cooldown, US regular session with opening/closing skip windows,
   position-count and daily-loss caps, a **staleness** check (skip if the price already
   moved more than `max_stale_move_pct` since the story was first seen), and a story-age
   limit.
5. **Execute** paper market orders. Stops, targets and a time exit are tracked locally and
   checked every cycle against the live quote; exits are reduce-only sells.
6. **Journal** everything. `report` prints the day; `--publish` posts it to Notes.

## Running it

Use the terminal's own interpreter so the environment matches, from `fincept-qt/scripts`:

```bash
PY="$HOME/Library/Application Support/com.fincept.terminal/venv-numpy2/bin/python3"
cd fincept-qt/scripts
"$PY" -m headline_algo check
"$PY" -m headline_algo once --dry-run
"$PY" -m headline_algo run --dry-run        # loop, decisions only
"$PY" -m headline_algo run                  # loop, paper orders
"$PY" -m headline_algo report --publish
```

Prerequisites: the terminal is running, its MCP server is started (Agent Workbench →
CONNECT → START, tick "Start with the terminal"), and "Allow external agents to run
actions" is on so paper orders and notes are accepted. The token is picked up from
`FINCEPT_MCP_TOKEN`, a `token_file` in the config, or the entry "ADD TO CLAUDE CODE"
wrote to `~/.claude.json`.

Copy `config.example.json`, edit, and pass `--config path.json`. Unset keys keep their
defaults (see `config.py`).

## Extraction backends

| backend     | needs                               | notes |
|-------------|-------------------------------------|-------|
| `rules`     | nothing                             | keyword + terminal-tag heuristics, confidence capped at 0.8. Default and fallback. |
| `claude`    | `claude login` once on this machine | shells out to `claude -p` on your subscription; model via `extract.model`. |
| `anthropic` | `ANTHROPIC_API_KEY`                 | direct Messages API; model via `extract.anthropic_model`. |

LLM output is validated against the schema; anything malformed falls back to rules and
the journal records which backend produced the event.

## What to measure before anything goes live

Run for two to four weeks in paper and read the journal, not the P&L:

- hit rate and average move captured **by event type**;
- the move from first headline to our entry (how late we are), and how often the
  staleness rule fires;
- how many trades the daily-loss and position caps blocked;
- which skips were wrong in hindsight.

Expect most event types to show no edge. Keep the ones that do; drop the rest.

## Safety

- Paper only. There is no code path to a live broker in this package.
- The terminal skips its own `risk.*` limits for the local paper simulator, so the
  pipeline enforces its own copy (`risk` section). Keep both in sync.
- Long-only in phase one: bearish events close an existing long or buy a defensive ETF;
  they never open shorts.
- State lives in `~/Library/Application Support/com.fincept.terminal/headline_algo.db`;
  delete it to start the journal fresh.

## Tests

```bash
"$PY" -m pytest headline_algo/tests -q
```
