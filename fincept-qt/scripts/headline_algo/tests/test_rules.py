from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from headline_algo.config import Config
from headline_algo.extract import Event
from headline_algo.mapping import Candidate
from headline_algo.rules import decide_entry, exit_reason, market_open, size_order
from headline_algo.state import State

ET = ZoneInfo("America/New_York")


def _cfg() -> Config:
    cfg = Config()
    cfg.state_db = ":memory:"
    return cfg


def _event(**kw) -> Event:
    base = dict(event_type="guidance_raise", direction="bullish", confidence=0.7)
    base.update(kw)
    return Event(**base)


def test_market_open_windows():
    cfg = _cfg()
    assert market_open(cfg, datetime(2026, 10, 28, 10, 0, tzinfo=ET))[0]
    assert not market_open(cfg, datetime(2026, 10, 28, 9, 32, tzinfo=ET))[0]      # opening skip window
    assert not market_open(cfg, datetime(2026, 10, 28, 15, 55, tzinfo=ET))[0]     # closing skip window
    assert not market_open(cfg, datetime(2026, 10, 31, 11, 0, tzinfo=ET))[0]      # Saturday
    assert not market_open(cfg, datetime(2026, 10, 28, 8, 0, tzinfo=ET))[0]


def test_size_order_respects_caps():
    cfg = _cfg()
    assert size_order(cfg, 100.0) == 100.0
    assert size_order(cfg, 5.0) == 1000.0            # max quantity cap
    assert size_order(cfg, 20_000.0) == 0.0          # one share exceeds max value
    assert size_order(cfg, 0.0) == 0.0


def test_decide_entry_happy_path_and_staleness():
    cfg = _cfg()
    state = State(":memory:")
    now = datetime(2026, 10, 28, 11, 0, tzinfo=ET)
    state.upsert_story("c:1", "cluster", "ACME raises guidance", 3, {})
    cand = Candidate(symbol="ACME", direction="bullish", why="ticker", price=50.0)
    d = decide_entry(cfg, state, "c:1", _event(), cand, open_count=0, day_pnl=0.0, now=now)
    assert d.action == "buy" and d.quantity == 200.0 and d.detail["move_since_first_pct"] == 0.0
    # price has since run 3% -> stale
    cand2 = Candidate(symbol="ACME", direction="bullish", why="ticker", price=51.5)
    d2 = decide_entry(cfg, state, "c:1", _event(), cand2, open_count=0, day_pnl=0.0, now=now)
    assert d2.action == "skip" and d2.reason.startswith("stale")


def test_decide_entry_gates():
    cfg = _cfg()
    state = State(":memory:")
    now = datetime(2026, 10, 28, 11, 0, tzinfo=ET)
    state.upsert_story("c:2", "cluster", "x", 3, {})
    cand = Candidate(symbol="XYZ", direction="bullish", why="ticker", price=10.0)
    assert decide_entry(cfg, state, "c:2", _event(confidence=0.3), cand, 0, 0.0, now).reason.startswith("confidence")
    assert "not tradeable" in decide_entry(cfg, state, "c:2", _event(event_type="other"), cand, 0, 0.0, now).reason
    assert "max open" in decide_entry(cfg, state, "c:2", _event(), cand, 8, 0.0, now).reason
    assert "daily loss" in decide_entry(cfg, state, "c:2", _event(), cand, 0, -2500.0, now).reason
    assert "market closed" in decide_entry(cfg, state, "c:2", _event(), cand, 0, 0.0,
                                           datetime(2026, 10, 28, 20, 0, tzinfo=ET)).reason
    bearish = Candidate(symbol="XYZ", direction="bearish", why="ticker", price=10.0)
    assert "long-only" in decide_entry(cfg, state, "c:2", _event(direction="bearish"), bearish, 0, 0.0, now).reason
    state.open_position("XYZ", "c:0", 10.0, 5, 9.8, 10.4, time.time() + 3600, None)
    assert decide_entry(cfg, state, "c:2", _event(direction="bearish"), bearish, 1, 0.0, now).action == "close"
    assert decide_entry(cfg, state, "c:2", _event(), cand, 1, 0.0, now).reason == "already holding"


def test_exit_reasons():
    cfg = _cfg()
    state = State(":memory:")
    state.open_position("ABC", "c:3", 100.0, 10, 98.0, 104.0, time.time() + 3600, None)
    pos = state.position("ABC")
    assert exit_reason(cfg, pos, 99.0) is None
    assert exit_reason(cfg, pos, 97.9) == "stop loss"
    assert exit_reason(cfg, pos, 104.5) == "take profit"
    assert exit_reason(cfg, pos, 99.0, now_ts=time.time() + 7200) == "time exit"
    state.close_position("ABC", 104.5, "take profit")
    assert state.position("ABC") is None
    assert state.realised_pnl_since(0) == 45.0
