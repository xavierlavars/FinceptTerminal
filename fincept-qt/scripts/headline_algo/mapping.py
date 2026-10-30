"""Mapping: an Event becomes a list of candidate instruments with a direction each.

Three sources, in order of trust:
  1. tickers the story or the extractor named, validated against a live quote;
  2. company names resolved through the terminal's symbol lookup (US listings only);
  3. macro targets turned into ETFs through macro_map.json, with per-event overrides
     (e.g. conflict escalation is bullish for defense and gold even though it is bad news).

Phase one is US-listed only: the paper engine fills on yfinance ticks and the
market-hours rule assumes the US regular session.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .extract import Event
from .terminal_client import TerminalClient

log = logging.getLogger("headline_algo.mapping")

US_EXCHANGES = {"NMS", "NYQ", "NGM", "NCM", "PCX", "BTS", "ASE", "NASDAQ", "NYSE", "NYSEARCA", "AMEX", "BATS"}


@dataclass
class Candidate:
    symbol: str
    direction: str          # bullish | bearish
    why: str                # ticker | company:<name> | macro:<target>
    price: float | None = None


class MacroMap:
    def __init__(self, path: str | Path):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.targets: dict[str, dict[str, str]] = {k: v for k, v in data.items()
                                                   if not k.startswith("_") and isinstance(v, dict)}
        self.overrides: dict[str, dict[str, str]] = data.get("_event_overrides") or {}

    def instruments(self, event: Event) -> list[Candidate]:
        out: list[Candidate] = []
        wanted: dict[str, str] = {}
        for t in event.macro_targets:
            if event.direction in ("bullish", "bearish"):
                wanted[t] = event.direction
        for t, d in (self.overrides.get(event.event_type) or {}).items():
            wanted[t] = d  # overrides win: they encode how the theme reacts to that event
        for target, direction in wanted.items():
            entry = self.targets.get(target) or {}
            # 'bullish' instrument is bought on bullish theme direction; the 'bearish' slot
            # holds a defensive/inverse instrument to buy when the theme is bearish.
            sym = entry.get(direction, "")
            if sym:
                out.append(Candidate(symbol=sym, direction="bullish", why=f"macro:{target}:{direction}"))
        return out


def _is_us_equity(hit: dict[str, Any]) -> bool:
    exch = str(hit.get("exchange") or "").upper()
    sym = str(hit.get("symbol") or "")
    typ = str(hit.get("type") or "").upper()
    if "." in sym or "=" in sym or "^" in sym:
        return False
    if typ and typ not in ("EQUITY", "ETF", "STOCK"):
        return False
    return (not exch) or exch in US_EXCHANGES


def _tradeable(symbol: str) -> bool:
    return symbol.isascii() and symbol.replace("-", "").isalnum() and "." not in symbol and 1 <= len(symbol) <= 6


def map_event(event: Event, client: TerminalClient, macro: MacroMap, *, max_candidates: int = 3) -> list[Candidate]:
    cands: list[Candidate] = []
    seen: set[str] = set()

    def add(c: Candidate) -> None:
        if c.symbol in seen or not _tradeable(c.symbol):
            return
        seen.add(c.symbol)
        cands.append(c)

    if event.direction in ("bullish", "bearish"):
        for t in event.tickers:
            add(Candidate(symbol=t, direction=event.direction, why="ticker"))
        for name in event.companies[:3]:
            try:
                hits = client.lookup_symbol(name, limit=5)
            except Exception as exc:  # noqa: BLE001 - lookup is best effort
                log.info("lookup %r failed: %s", name, exc)
                continue
            for h in hits:
                if _is_us_equity(h):
                    add(Candidate(symbol=str(h["symbol"]).upper(), direction=event.direction,
                                  why=f"company:{name}"))
                    break
    for c in macro.instruments(event):
        add(c)

    # Validate against a live quote: a symbol the terminal cannot price is not tradeable here.
    valid: list[Candidate] = []
    for c in cands:
        q = client.quote(c.symbol)
        if not q:
            log.info("dropping %s (%s): no quote", c.symbol, c.why)
            continue
        c.price = float(q["price"])
        valid.append(c)
        if len(valid) >= max_candidates:
            break
    return valid
