"""Rule layer: decide whether a mapped candidate becomes a paper order.

Every check returns a reason string so the journal can say exactly why a trade was
or was not taken. Order of checks is cheapest-first; the first failure wins.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, time as dtime
from typing import Any
from zoneinfo import ZoneInfo

from .config import Config
from .extract import Event
from .mapping import Candidate
from .state import State


@dataclass
class Decision:
    action: str          # buy | close | skip
    reason: str
    quantity: float = 0.0
    price: float | None = None
    detail: dict[str, Any] | None = None


def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def market_open(cfg: Config, now: datetime | None = None) -> tuple[bool, str]:
    tz = ZoneInfo(cfg.market_tz)
    now = (now or datetime.now(tz)).astimezone(tz)
    if now.weekday() >= 5:
        return False, "weekend"
    open_t, close_t = _parse_hhmm(cfg.session_open), _parse_hhmm(cfg.session_close)
    minutes = now.hour * 60 + now.minute
    o = open_t.hour * 60 + open_t.minute + cfg.skip_first_minutes
    c = close_t.hour * 60 + close_t.minute - cfg.skip_last_minutes
    if minutes < o:
        return False, "before session (or inside opening skip window)"
    if minutes >= c:
        return False, "after session (or inside closing skip window)"
    return True, "open"


def day_start_epoch(cfg: Config, now: datetime | None = None) -> float:
    tz = ZoneInfo(cfg.market_tz)
    now = (now or datetime.now(tz)).astimezone(tz)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def size_order(cfg: Config, price: float) -> float:
    """Whole-share quantity bounded by max order value and max quantity; 0 if a share is too dear."""
    if price <= 0:
        return 0.0
    qty = int(cfg.risk.max_order_value // price)
    return float(max(0, min(qty, cfg.risk.max_order_quantity)))


def decide_entry(cfg: Config, state: State, story_id: str, event: Event, cand: Candidate,
                 open_count: int, day_pnl: float, now: datetime | None = None) -> Decision:
    base: dict[str, Any] = {"why": cand.why, "event": event.event_type, "direction": event.direction,
                            "confidence": event.confidence, "backend": event.backend}
    if event.event_type not in cfg.signal.tradeable_events:
        return Decision("skip", f"event type {event.event_type} is not tradeable", detail=base)
    floor = cfg.signal.min_confidence_macro if cand.why.startswith("macro:") else cfg.signal.min_confidence
    if event.confidence < floor:
        return Decision("skip", f"confidence {event.confidence:.2f} < {floor:.2f}"
                                f"{' (macro floor)' if floor != cfg.signal.min_confidence else ''}", detail=base)
    if cand.direction == "bearish" and cfg.signal.long_only:
        pos = state.position(cand.symbol)
        if pos is not None:
            return Decision("close", "bearish event against an open long", price=cand.price,
                            quantity=float(pos["quantity"]), detail=base)
        return Decision("skip", "bearish on a name not held (long-only)", detail=base)
    if cand.price is None or cand.price <= 0:
        return Decision("skip", "no price", detail=base)
    if state.position(cand.symbol) is not None:
        return Decision("skip", "already holding", detail=base)
    if state.in_cooldown(cand.symbol):
        return Decision("skip", "symbol in cooldown", detail=base)
    if state.already_decided(story_id, cand.symbol):
        return Decision("skip", "already acted on this story for this symbol", detail=base)
    ok, why = market_open(cfg, now)
    if not ok:
        return Decision("skip", f"market closed: {why}", detail=base)
    if open_count >= cfg.risk.max_open_positions:
        return Decision("skip", f"max open positions ({cfg.risk.max_open_positions}) reached", detail=base)
    if day_pnl <= -abs(cfg.risk.daily_loss_limit):
        return Decision("skip", f"daily loss limit hit ({day_pnl:.0f})", detail=base)
    # Staleness: how far has the price run since we first saw the story?
    first = state.first_price(story_id, cand.symbol)
    if first is None:
        state.record_first_price(story_id, cand.symbol, cand.price)
        first = cand.price
    move_pct = (cand.price - first) / first * 100.0 if first else 0.0
    base["first_price"] = first
    base["move_since_first_pct"] = round(move_pct, 3)
    if abs(move_pct) > cfg.signal.max_stale_move_pct:
        return Decision("skip", f"stale: already moved {move_pct:+.2f}% since first seen", detail=base)
    # Age: a story first seen long ago is not news anymore.
    first_seen = state.story_first_seen(story_id) or time.time()
    age_min = (time.time() - first_seen) / 60.0
    base["story_age_min"] = round(age_min, 1)
    if age_min > 180:
        return Decision("skip", f"story is {age_min:.0f} min old", detail=base)
    qty = size_order(cfg, cand.price)
    if qty < 1:
        return Decision("skip", f"price {cand.price:.2f} exceeds max order value", detail=base)
    return Decision("buy", "all checks passed", quantity=qty, price=cand.price, detail=base)


def exit_reason(cfg: Config, pos: Any, last_price: float | None, now_ts: float | None = None) -> str | None:
    """Which exit, if any, fires for an open position row."""
    now_ts = now_ts or time.time()
    if now_ts >= float(pos["exit_by"]):
        return "time exit"
    if last_price is None:
        return None
    if last_price <= float(pos["stop_price"]):
        return "stop loss"
    if last_price >= float(pos["target_price"]):
        return "take profit"
    return None
