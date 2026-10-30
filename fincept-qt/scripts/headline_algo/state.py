"""SQLite state: what we've seen, what we decided, what we hold.

The decision log is the product of phase one. Every cluster that reaches the rule
layer gets a row, including the ones that were skipped and why.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS stories (
    story_id      TEXT PRIMARY KEY,          -- cluster id or alert id
    kind          TEXT NOT NULL,             -- cluster | alert | geo
    first_seen    REAL NOT NULL,
    last_seen     REAL NOT NULL,
    source_count  INTEGER NOT NULL DEFAULT 1,
    headline      TEXT NOT NULL,
    raw           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS story_articles (
    article_id    TEXT PRIMARY KEY,
    story_id      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS first_prices (
    story_id      TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    price         REAL NOT NULL,
    seen_at       REAL NOT NULL,
    PRIMARY KEY (story_id, symbol)
);
CREATE TABLE IF NOT EXISTS events (
    story_id      TEXT PRIMARY KEY,
    extracted_at  REAL NOT NULL,
    backend       TEXT NOT NULL,
    event_json    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    decided_at    REAL NOT NULL,
    story_id      TEXT NOT NULL,
    symbol        TEXT,
    event_type    TEXT,
    direction     TEXT,
    confidence    REAL,
    action        TEXT NOT NULL,             -- buy | close | skip
    reason        TEXT NOT NULL,
    detail_json   TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    symbol        TEXT PRIMARY KEY,
    story_id      TEXT NOT NULL,
    opened_at     REAL NOT NULL,
    entry_price   REAL NOT NULL,
    quantity      REAL NOT NULL,
    stop_price    REAL NOT NULL,
    target_price  REAL NOT NULL,
    exit_by       REAL NOT NULL,
    order_id      TEXT,
    status        TEXT NOT NULL DEFAULT 'open'  -- open | closing | closed
);
CREATE TABLE IF NOT EXISTS closed_trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    story_id      TEXT NOT NULL,
    opened_at     REAL NOT NULL,
    closed_at     REAL NOT NULL,
    entry_price   REAL NOT NULL,
    exit_price    REAL,
    quantity      REAL NOT NULL,
    exit_reason   TEXT NOT NULL,
    pnl           REAL
);
CREATE TABLE IF NOT EXISTS cooldowns (
    symbol        TEXT PRIMARY KEY,
    until         REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key           TEXT PRIMARY KEY,
    value         TEXT NOT NULL
);
"""


def _dump(obj: Any) -> str:
    if is_dataclass(obj) and not isinstance(obj, type):
        obj = asdict(obj)
    return json.dumps(obj, default=str, ensure_ascii=False)


class State:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self._db.execute("BEGIN")
        try:
            yield self._db
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    # -- stories -------------------------------------------------------------------

    def upsert_story(self, story_id: str, kind: str, headline: str, source_count: int, raw: Any) -> bool:
        """Returns True when the story is new."""
        now = time.time()
        row = self._db.execute("SELECT story_id FROM stories WHERE story_id=?", (story_id,)).fetchone()
        if row:
            self._db.execute("UPDATE stories SET last_seen=?, source_count=? WHERE story_id=?",
                             (now, source_count, story_id))
            return False
        self._db.execute(
            "INSERT INTO stories(story_id, kind, first_seen, last_seen, source_count, headline, raw) "
            "VALUES (?,?,?,?,?,?,?)", (story_id, kind, now, now, source_count, headline, _dump(raw)))
        return True

    def canonical_story_id(self, candidate_id: str, article_ids: list[str]) -> str:
        """The id we already know this story by, if any of its articles were seen before.

        Cluster ids are not stable between calls, so identity is anchored on article ids.
        """
        for aid in article_ids:
            row = self._db.execute("SELECT story_id FROM story_articles WHERE article_id=?", (aid,)).fetchone()
            if row:
                return str(row[0])
        return candidate_id

    def bind_articles(self, story_id: str, article_ids: list[str]) -> None:
        self._db.executemany("INSERT OR IGNORE INTO story_articles(article_id, story_id) VALUES (?,?)",
                             [(aid, story_id) for aid in article_ids])

    def story_first_seen(self, story_id: str) -> float | None:
        row = self._db.execute("SELECT first_seen FROM stories WHERE story_id=?", (story_id,)).fetchone()
        return float(row[0]) if row else None

    def record_first_price(self, story_id: str, symbol: str, price: float) -> None:
        self._db.execute("INSERT OR IGNORE INTO first_prices(story_id, symbol, price, seen_at) VALUES (?,?,?,?)",
                         (story_id, symbol, price, time.time()))

    def first_price(self, story_id: str, symbol: str) -> float | None:
        row = self._db.execute("SELECT price FROM first_prices WHERE story_id=? AND symbol=?",
                               (story_id, symbol)).fetchone()
        return float(row[0]) if row else None

    # -- events --------------------------------------------------------------------

    def has_event(self, story_id: str) -> bool:
        return self._db.execute("SELECT 1 FROM events WHERE story_id=?", (story_id,)).fetchone() is not None

    def save_event(self, story_id: str, backend: str, event: Any) -> None:
        self._db.execute("INSERT OR REPLACE INTO events(story_id, extracted_at, backend, event_json) VALUES (?,?,?,?)",
                         (story_id, time.time(), backend, _dump(event)))

    def load_event(self, story_id: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT event_json FROM events WHERE story_id=?", (story_id,)).fetchone()
        return json.loads(row[0]) if row else None

    # -- decisions -----------------------------------------------------------------

    def log_decision(self, story_id: str, action: str, reason: str, *, symbol: str | None = None,
                     event_type: str | None = None, direction: str | None = None,
                     confidence: float | None = None, detail: Any = None) -> None:
        self._db.execute(
            "INSERT INTO decisions(decided_at, story_id, symbol, event_type, direction, confidence, action, reason, "
            "detail_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (time.time(), story_id, symbol, event_type, direction, confidence, action, reason,
             _dump(detail) if detail is not None else None))

    def decisions_since(self, since: float) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM decisions WHERE decided_at>=? ORDER BY decided_at", (since,)).fetchall()

    def skip_logged(self, story_id: str, symbol: str | None, reason: str) -> bool:
        """True when this exact skip (story, symbol, reason prefix) is already in the journal."""
        prefix = reason.split(":")[0]
        return self._db.execute(
            "SELECT 1 FROM decisions WHERE story_id=? AND COALESCE(symbol,'')=? AND action='skip' "
            "AND substr(reason, 1, length(?)) = ?", (story_id, symbol or "", prefix, prefix)).fetchone() is not None

    def already_decided(self, story_id: str, symbol: str) -> bool:
        return self._db.execute(
            "SELECT 1 FROM decisions WHERE story_id=? AND symbol=? AND action IN ('buy','close')",
            (story_id, symbol)).fetchone() is not None

    # -- positions -----------------------------------------------------------------

    def open_positions(self) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM positions WHERE status='open' ORDER BY opened_at").fetchall()

    def position(self, symbol: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM positions WHERE symbol=? AND status!='closed'", (symbol,)).fetchone()

    def open_position(self, symbol: str, story_id: str, entry_price: float, quantity: float, stop_price: float,
                      target_price: float, exit_by: float, order_id: str | None) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO positions(symbol, story_id, opened_at, entry_price, quantity, stop_price, "
            "target_price, exit_by, order_id, status) VALUES (?,?,?,?,?,?,?,?,?,'open')",
            (symbol, story_id, time.time(), entry_price, quantity, stop_price, target_price, exit_by, order_id))

    def close_position(self, symbol: str, exit_price: float | None, exit_reason: str) -> None:
        row = self.position(symbol)
        if not row:
            return
        pnl = None if exit_price is None else (exit_price - row["entry_price"]) * row["quantity"]
        with self.tx() as db:
            db.execute(
                "INSERT INTO closed_trades(symbol, story_id, opened_at, closed_at, entry_price, exit_price, quantity, "
                "exit_reason, pnl) VALUES (?,?,?,?,?,?,?,?,?)",
                (symbol, row["story_id"], row["opened_at"], time.time(), row["entry_price"], exit_price,
                 row["quantity"], exit_reason, pnl))
            db.execute("UPDATE positions SET status='closed' WHERE symbol=?", (symbol,))

    def closed_trades_since(self, since: float) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM closed_trades WHERE closed_at>=? ORDER BY closed_at", (since,)).fetchall()

    def realised_pnl_since(self, since: float) -> float:
        row = self._db.execute("SELECT COALESCE(SUM(pnl),0) FROM closed_trades WHERE closed_at>=?", (since,)).fetchone()
        return float(row[0] or 0.0)

    # -- cooldowns / meta ------------------------------------------------------------

    def set_cooldown(self, symbol: str, until: float) -> None:
        self._db.execute("INSERT OR REPLACE INTO cooldowns(symbol, until) VALUES (?,?)", (symbol, until))

    def in_cooldown(self, symbol: str) -> bool:
        row = self._db.execute("SELECT until FROM cooldowns WHERE symbol=?", (symbol,)).fetchone()
        return bool(row and float(row[0]) > time.time())

    def get_meta(self, key: str, default: str = "") -> str:
        row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, value))
