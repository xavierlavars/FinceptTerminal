"""headline_algo — phase-one, paper-only headline trading pipeline for Fincept Terminal.

Layers (one module each, testable on their own):

    ingest   -> terminal_client   news clusters + threat alerts (+ optional geopolitics)
    extract  -> Event             fixed-schema event extraction (rules | claude CLI | anthropic API)
    mapping  -> instruments       entities / macro targets -> US-listed tickers and ETFs
    rules    -> Decision          gating, staleness, caps, market hours
    execute  -> paper orders      entries, stops, time exits through the terminal's paper engine
    journal  -> SQLite + notes    every decision, taken or skipped, with the reason

Nothing here talks to a live broker. Everything goes through the terminal's MCP server,
the same interface an external AI agent uses, so the terminal's own approval rules apply.
"""

__version__ = "0.1.0"
