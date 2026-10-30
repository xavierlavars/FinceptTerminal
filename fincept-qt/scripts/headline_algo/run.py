"""Pipeline orchestration and CLI.

    python -m headline_algo check                 # connectivity + portfolio + config echo
    python -m headline_algo once [--dry-run]      # one full cycle
    python -m headline_algo run  [--dry-run]      # loop every poll_interval_sec
    python -m headline_algo status
    python -m headline_algo report [--publish]    # today's journal (optionally to Notes)

Run it with the terminal's own interpreter so the environment matches:
    ~/Library/Application\\ Support/com.fincept.terminal/venv-numpy2/bin/python3 -m headline_algo ...
from the fincept-qt/scripts directory (or with PYTHONPATH pointing at it).
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any

from .config import Config, load_config, resolve_token
from .execute import enter, close, manage_exits, reconcile
from .extract import RULES_TAG, Event, extract, validate_event
from .ingest import Story, fetch_stories, register_stories
from .journal import daily_report, publish_daily_note, status_text
from .mapping import MacroMap, map_event
from .rules import day_start_epoch, decide_entry
from .state import State
from .terminal_client import TerminalClient, TerminalError, TerminalUnavailable

log = logging.getLogger("headline_algo")

# Single-source alerts are only considered when the terminal is fairly sure they matter.
ALERT_MIN_CONFIDENCE = 0.75
ALERT_CATEGORIES = {"market", "regulatory", "cyber", "conflict"}


def setup_logging(level: str, data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format=fmt,
                        handlers=[logging.StreamHandler(sys.stderr),
                                  logging.FileHandler(data_dir / "headline_algo.log", encoding="utf-8")])


def resolve_portfolio(cfg: Config, client: TerminalClient) -> str:
    if cfg.portfolio_id:
        return cfg.portfolio_id
    for p in client.paper_portfolios():
        if str(p.get("name", "")).strip().lower() == cfg.portfolio_name.strip().lower():
            cfg.portfolio_id = str(p["id"])
            return cfg.portfolio_id
    raise TerminalError(f"paper portfolio {cfg.portfolio_name!r} not found; set portfolio_id in the config")


def eligible(cfg: Config, story: Story) -> str | None:
    """Cheap pre-filter before any extraction. Returns a skip reason or None."""
    if story.kind == "cluster" and story.source_count < cfg.signal.min_sources:
        return f"only {story.source_count} source(s)"
    if story.kind == "alert":
        if story.threat_category not in ALERT_CATEGORIES:
            return f"alert category {story.threat_category or 'n/a'} not actionable"
        if story.threat_confidence < ALERT_MIN_CONFIDENCE:
            return f"alert confidence {story.threat_confidence:.2f} < {ALERT_MIN_CONFIDENCE:.2f}"
    if story.kind == "geo":
        return "geopolitics feed items are logged only in phase one"
    return None


def get_event(cfg: Config, state: State, story: Story) -> Event:
    cached = state.load_event(story.story_id)
    if cached:
        backend = str(cached.get("backend") or "rules")
        stale_rules = backend.startswith("rules") and backend != RULES_TAG
        ev = None if stale_rules else validate_event(cached, backend)
        if ev:
            return ev
    ev = extract(story, cfg.extract)
    state.save_event(story.story_id, ev.backend, ev.to_dict())
    return ev


def cycle(cfg: Config, state: State, client: TerminalClient, macro: MacroMap, *, dry_run: bool) -> dict[str, Any]:
    started = time.time()
    summary: dict[str, Any] = {"stories": 0, "new": 0, "considered": 0, "buys": 0, "closes": 0, "skips": 0,
                               "exits": 0, "errors": 0}
    stories = fetch_stories(client, cfg)
    new, updated = register_stories(state, stories)
    summary["stories"], summary["new"] = len(stories), len(new)
    open_count = len(state.open_positions())
    day_pnl = state.realised_pnl_since(day_start_epoch(cfg))

    for story in stories:
        try:
            why = eligible(cfg, story)
            if why:
                if story in new:
                    state.log_decision(story.story_id, "skip", why, detail={"headline": story.headline})
                    summary["skips"] += 1
                continue
            event = get_event(cfg, state, story)
            summary["considered"] += 1
            if event.event_type == "other" or event.direction == "neutral":
                if story in new:
                    state.log_decision(story.story_id, "skip", f"no actionable event ({event.event_type}/{event.direction})",
                                       event_type=event.event_type, direction=event.direction,
                                       confidence=event.confidence, detail={"headline": story.headline})
                    summary["skips"] += 1
                continue
            cands = map_event(event, client, macro)
            if not cands:
                if story in new:
                    state.log_decision(story.story_id, "skip", "no tradeable instrument", event_type=event.event_type,
                                       direction=event.direction, confidence=event.confidence,
                                       detail={"headline": story.headline, "tickers": event.tickers,
                                               "companies": event.companies, "macro": event.macro_targets})
                    summary["skips"] += 1
                continue
            for cand in cands:
                if state.already_decided(story.story_id, cand.symbol):
                    continue
                d = decide_entry(cfg, state, story.story_id, event, cand, open_count, day_pnl)
                detail = dict(d.detail or {})
                detail["headline"] = story.headline
                if d.action == "buy":
                    info = enter(cfg, state, client, story.story_id, cand.symbol, d, dry_run=dry_run)
                    detail["execution"] = info
                    if not dry_run:
                        open_count += 1
                    summary["buys"] += 1
                elif d.action == "close":
                    info = close(cfg, state, client, cand.symbol, d.reason, cand.price, dry_run=dry_run)
                    detail["execution"] = info
                    summary["closes"] += 1
                else:
                    if state.skip_logged(story.story_id, cand.symbol, d.reason):
                        continue  # same story, same symbol, same reason: already in the journal
                    summary["skips"] += 1
                state.log_decision(story.story_id, d.action, d.reason, symbol=cand.symbol,
                                   event_type=event.event_type, direction=event.direction,
                                   confidence=event.confidence, detail=detail)
        except TerminalUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad story must not stop the cycle
            summary["errors"] += 1
            log.exception("story %s failed: %s", story.story_id, exc)

    exits = manage_exits(cfg, state, client, dry_run=dry_run)
    summary["exits"] = len(exits)
    for note in reconcile(cfg, state, client):
        log.warning(note)
    if cfg.daily_note and not dry_run:
        publish_daily_note(cfg, state, client)
    state.set_meta("last_cycle_at", time.strftime("%Y-%m-%d %H:%M:%S"))
    summary["seconds"] = round(time.time() - started, 1)
    log.info("cycle: %s", summary)
    return summary


def build(args: argparse.Namespace) -> tuple[Config, State, TerminalClient, MacroMap]:
    cfg = load_config(args.config)
    if args.extract:
        cfg.extract.backend = args.extract
    setup_logging(args.log_level, Path(cfg.state_db).parent)
    client = TerminalClient(cfg.terminal_url, resolve_token(cfg))
    client.initialize()
    resolve_portfolio(cfg, client)
    state = State(cfg.state_db)
    return cfg, state, client, MacroMap(cfg.macro_map_file)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="headline_algo", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["check", "once", "run", "status", "report"])
    ap.add_argument("--config", help="JSON config file (see config.example.json)")
    ap.add_argument("--dry-run", action="store_true", help="decide and log, but place no orders")
    ap.add_argument("--extract", choices=["rules", "claude", "anthropic"], help="override extract.backend")
    ap.add_argument("--publish", action="store_true", help="report: also post to the terminal's Notes")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    try:
        cfg, state, client, macro = build(args)
    except (TerminalError, RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.command == "check":
        print(f"terminal: {cfg.terminal_url} OK")
        print(f"portfolio: {cfg.portfolio_name} ({cfg.portfolio_id})")
        print(f"extract backend: {cfg.extract.backend}")
        print(f"state db: {cfg.state_db}")
        print(f"risk: {cfg.risk}")
        print(f"signal: min_sources={cfg.signal.min_sources} min_conf={cfg.signal.min_confidence} "
              f"max_stale_move={cfg.signal.max_stale_move_pct}% long_only={cfg.signal.long_only}")
        return 0
    if args.command == "status":
        print(status_text(cfg, state))
        return 0
    if args.command == "report":
        print(daily_report(cfg, state))
        if args.publish:
            ok = publish_daily_note(cfg, state, client, force=True)
            print("published to Notes" if ok else "publish failed")
        return 0
    if args.command == "once":
        cycle(cfg, state, client, macro, dry_run=args.dry_run)
        return 0

    stop = {"flag": False}

    def _sig(*_: Any) -> None:
        stop["flag"] = True
        log.info("stopping after this cycle")

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    log.info("loop every %ss (%s)", cfg.poll_interval_sec, "DRY RUN" if args.dry_run else "paper orders live")
    backoff = cfg.poll_interval_sec
    while not stop["flag"]:
        try:
            cycle(cfg, state, client, macro, dry_run=args.dry_run)
            backoff = cfg.poll_interval_sec
        except TerminalUnavailable as exc:
            log.warning("terminal unavailable: %s — retry in %ss", exc, backoff)
            backoff = min(backoff * 2, 600)
        except Exception as exc:  # noqa: BLE001
            log.exception("cycle failed: %s", exc)
        for _ in range(int(backoff)):
            if stop["flag"]:
                break
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
