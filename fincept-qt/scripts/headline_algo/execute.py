"""Execution against the terminal's paper engine.

Entries are market orders with the current price as margin reference (the engine
requires it). Exits are reduce-only market sells. Stops and targets live in our
state, not in the engine, because the paper matcher has no bracket orders; the
loop checks them every cycle against the live quote.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from .config import Config
from .rules import Decision, exit_reason
from .state import State
from .terminal_client import TerminalClient, TerminalError

log = logging.getLogger("headline_algo.execute")


def enter(cfg: Config, state: State, client: TerminalClient, story_id: str, symbol: str, decision: Decision,
          *, dry_run: bool) -> dict[str, Any]:
    assert decision.action == "buy" and decision.price
    price = decision.price
    stop = round(price * (1 - cfg.exit.stop_loss_pct / 100.0), 4)
    target = round(price * (1 + cfg.exit.take_profit_pct / 100.0), 4)
    exit_by = time.time() + cfg.exit.max_hold_hours * 3600.0
    info: dict[str, Any] = {"symbol": symbol, "quantity": decision.quantity, "price": price,
                            "stop": stop, "target": target, "exit_by": exit_by, "dry_run": dry_run}
    if dry_run:
        log.info("DRY RUN buy %s x%.0f @ %.2f (stop %.2f, target %.2f)", symbol, decision.quantity, price, stop, target)
        return info
    order = client.paper_order(cfg.portfolio_id, symbol, "buy", decision.quantity, price)
    info["order"] = order
    state.open_position(symbol, story_id, price, decision.quantity, stop, target, exit_by, order.get("order_id"))
    state.set_cooldown(symbol, time.time() + cfg.risk.per_symbol_cooldown_min * 60.0)
    log.info("BUY %s x%.0f @ %.2f -> order %s", symbol, decision.quantity, price, order.get("order_id"))
    return info


def close(cfg: Config, state: State, client: TerminalClient, symbol: str, reason: str, last_price: float | None,
          *, dry_run: bool) -> dict[str, Any]:
    pos = state.position(symbol)
    if pos is None:
        return {"symbol": symbol, "skipped": "no position"}
    qty = float(pos["quantity"])
    ref = last_price or float(pos["entry_price"])
    info: dict[str, Any] = {"symbol": symbol, "quantity": qty, "price": ref, "reason": reason, "dry_run": dry_run}
    if dry_run:
        log.info("DRY RUN close %s x%.0f @ %.2f (%s)", symbol, qty, ref, reason)
        return info
    try:
        order = client.paper_order(cfg.portfolio_id, symbol, "sell", qty, ref, reduce_only=True)
    except TerminalError as exc:
        log.warning("close %s failed: %s", symbol, exc)
        info["error"] = str(exc)
        return info
    info["order"] = order
    state.close_position(symbol, ref, reason)
    log.info("CLOSE %s x%.0f @ %.2f (%s) -> order %s", symbol, qty, ref, reason, order.get("order_id"))
    return info


def manage_exits(cfg: Config, state: State, client: TerminalClient, *, dry_run: bool) -> list[dict[str, Any]]:
    """Check every open position against stop, target and time exit."""
    actions: list[dict[str, Any]] = []
    for pos in state.open_positions():
        symbol = pos["symbol"]
        q = client.quote(symbol)
        last = float(q["price"]) if q else None
        why = exit_reason(cfg, pos, last)
        if not why:
            continue
        info = close(cfg, state, client, symbol, why, last, dry_run=dry_run)
        state.log_decision(pos["story_id"], "close", why, symbol=symbol, detail=info)
        actions.append(info)
    return actions


def reconcile(cfg: Config, state: State, client: TerminalClient) -> list[str]:
    """Flag positions the engine no longer shows (filled closes, manual intervention)."""
    notes: list[str] = []
    try:
        engine = {str(p.get("symbol")).upper(): p for p in client.paper_positions(cfg.portfolio_id)}
    except TerminalError as exc:
        return [f"reconcile skipped: {exc}"]
    for pos in state.open_positions():
        sym = pos["symbol"].upper()
        ep = engine.get(sym)
        if ep is None:
            # Market orders fill on the next tick; give a fresh entry a few minutes before worrying.
            if time.time() - float(pos["opened_at"]) > 900:
                notes.append(f"{sym}: tracked as open but absent from the paper engine (closed manually or never filled)")
                state.close_position(sym, None, "missing in engine")
    return notes
