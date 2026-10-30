"""Ingestion: pull stories from the terminal and normalise them.

Sources, cheapest first:
  * news clusters   - multi-source stories with velocity and a breaking flag (local, free)
  * threat alerts   - single articles the terminal classified HIGH/CRITICAL (local, free)
  * geopolitics     - Fincept's conflict monitor (credit-metered; off unless enabled)

Everything is reduced to a Story so the rest of the pipeline never sees source quirks.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .state import State
from .terminal_client import TerminalClient, TerminalError

log = logging.getLogger("headline_algo.ingest")

# The terminal's ticker tagger is keyword based and produces tokens like IT, EV, DR,
# PCE, RBA. Those are not tradeable symbols; drop them before anything trusts them.
JUNK_TICKERS = {
    "IT", "EV", "DR", "PCE", "RBA", "ECB", "BOJ", "FED", "GDP", "CPI", "AI", "US", "UK", "EU", "UN",
    "CEO", "CFO", "IPO", "ETF", "SEC", "OFAC", "XAU", "BTC", "ETH", "BYD", "MEXC", "ABC", "SBS",
}


@dataclass
class Story:
    story_id: str
    kind: str                      # cluster | alert | geo
    headline: str
    summary: str
    category: str                  # terminal category, e.g. EARNINGS, ECONOMIC, GEOPOLITICS
    sentiment: str                 # BULLISH | BEARISH | NEUTRAL
    impact: str                    # LOW | MEDIUM | HIGH
    priority: str                  # ROUTINE | BREAKING | URGENT | FLASH
    source_count: int
    velocity: str                  # rising | stable | falling | ""
    is_breaking: bool
    tickers: list[str]
    threat_category: str = ""
    threat_confidence: float = 0.0
    article_ids: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    headlines: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def text(self, max_chars: int = 1800) -> str:
        """Compact text block for the extractor."""
        lines = [f"HEADLINE: {self.headline}"]
        if self.summary:
            lines.append(f"SUMMARY: {self.summary}")
        for h in self.headlines[1:6]:
            lines.append(f"ALSO: {h}")
        lines.append(f"CATEGORY: {self.category} | SOURCES: {self.source_count} | VELOCITY: {self.velocity or 'n/a'}"
                     f" | TERMINAL_SENTIMENT: {self.sentiment} | IMPACT: {self.impact}")
        if self.tickers:
            lines.append("TAGGED_TICKERS: " + ", ".join(self.tickers))
        return "\n".join(lines)[:max_chars]


def _clean_tickers(raw: Any) -> list[str]:
    out: list[str] = []
    for t in raw or []:
        t = str(t).strip().upper()
        if not t or t in JUNK_TICKERS or len(t) > 6 or not t.replace(".", "").replace("-", "").isalnum():
            continue
        if t not in out:
            out.append(t)
    return out


def story_from_cluster(c: dict[str, Any]) -> Story:
    lead = c.get("lead") or {}
    arts = c.get("articles") or ([lead] if lead else [])
    tickers: list[str] = []
    sources: list[str] = []
    headlines: list[str] = []
    links: list[str] = []
    article_ids: list[str] = [str(a["id"]) for a in arts if a.get("id")]
    best_summary = str(lead.get("summary") or "")
    impact_rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    impact = str(lead.get("impact") or "LOW")
    priority = str(lead.get("priority") or "ROUTINE")
    for a in arts:
        for t in _clean_tickers(a.get("tickers")):
            if t not in tickers:
                tickers.append(t)
        if a.get("source") and a["source"] not in sources:
            sources.append(str(a["source"]))
        if a.get("headline"):
            headlines.append(str(a["headline"]))
        if a.get("link"):
            links.append(str(a["link"]))
        if len(str(a.get("summary") or "")) > len(best_summary):
            best_summary = str(a["summary"])
        if impact_rank.get(str(a.get("impact")), 0) > impact_rank.get(impact, 0):
            impact = str(a["impact"])
        if str(a.get("priority")) in ("FLASH", "URGENT", "BREAKING") and priority == "ROUTINE":
            priority = str(a["priority"])
    # The terminal's cluster id changes from call to call; the lead article id does not.
    anchor = str(lead.get("id") or (article_ids[0] if article_ids else "")) or \
        hashlib.sha1(str(lead.get("headline")).encode()).hexdigest()[:10]
    return Story(
        story_id="c:" + anchor,
        kind="cluster",
        headline=str(lead.get("headline") or (headlines[0] if headlines else "")),
        summary=best_summary,
        category=str(c.get("category") or lead.get("category") or "GENERAL"),
        sentiment=str(c.get("sentiment") or lead.get("sentiment") or "NEUTRAL"),
        impact=impact,
        priority=priority,
        source_count=int(c.get("source_count") or len(sources) or 1),
        velocity=str(c.get("velocity") or ""),
        is_breaking=bool(c.get("is_breaking")),
        tickers=tickers,
        article_ids=article_ids,
        sources=sources,
        headlines=headlines or [str(lead.get("headline") or "")],
        links=links,
        raw=c,
    )


def story_from_alert(a: dict[str, Any]) -> Story:
    return Story(
        story_id="a:" + str(a.get("id") or hashlib.sha1(str(a.get("headline")).encode()).hexdigest()[:10]),
        kind="alert",
        headline=str(a.get("headline") or ""),
        summary=str(a.get("summary") or ""),
        category=str(a.get("category") or "GENERAL"),
        sentiment=str(a.get("sentiment") or "NEUTRAL"),
        impact=str(a.get("impact") or "LOW"),
        priority=str(a.get("priority") or "ROUTINE"),
        source_count=1,
        velocity="",
        is_breaking=str(a.get("priority")) in ("FLASH", "URGENT", "BREAKING"),
        tickers=_clean_tickers(a.get("tickers")),
        threat_category=str(a.get("threat_category") or ""),
        threat_confidence=float(a.get("threat_confidence") or 0.0),
        article_ids=[str(a["id"])] if a.get("id") else [],
        sources=[str(a.get("source") or "")],
        headlines=[str(a.get("headline") or "")],
        links=[str(a.get("link") or "")] if a.get("link") else [],
        raw=a,
    )


def story_from_geo(e: dict[str, Any]) -> Story:
    key = str(e.get("url") or e.get("title") or "")
    return Story(
        story_id="g:" + hashlib.sha1(key.encode()).hexdigest()[:12],
        kind="geo",
        headline=str(e.get("title") or ""),
        summary="",
        category="GEOPOLITICS",
        sentiment="NEUTRAL",
        impact="MEDIUM",
        priority="ROUTINE",
        source_count=1,
        velocity="",
        is_breaking=False,
        tickers=[],
        threat_category=str(e.get("event_category") or ""),
        sources=[str(e.get("source") or "")],
        headlines=[str(e.get("title") or "")],
        links=[str(e.get("url") or "")] if e.get("url") else [],
        raw=e,
    )


def fetch_stories(client: TerminalClient, cfg: Config) -> list[Story]:
    stories: dict[str, Story] = {}
    try:
        for c in client.news_clusters(cfg.cluster_time_range, cfg.signal.min_sources, cfg.cluster_limit):
            s = story_from_cluster(c)
            stories[s.story_id] = s
    except TerminalError as exc:
        log.warning("news clusters unavailable: %s", exc)
    seen_headlines = {s.headline.strip().lower() for s in stories.values()}
    seen_articles = {aid for s in stories.values() for aid in s.article_ids}
    try:
        for a in client.threat_alerts(cfg.cluster_time_range):
            s = story_from_alert(a)
            # An alert that is already part of a cluster adds nothing; keep the cluster.
            if s.headline.strip().lower() in seen_headlines or set(s.article_ids) & seen_articles:
                continue
            stories[s.story_id] = s
    except TerminalError as exc:
        log.warning("threat alerts unavailable: %s", exc)
    if cfg.use_geopolitics_feed:
        try:
            for e in client.geopolitics_events(limit=30):
                s = story_from_geo(e)
                stories[s.story_id] = s
        except TerminalError as exc:
            log.warning("geopolitics feed unavailable: %s", exc)
    return list(stories.values())


def register_stories(state: State, stories: list[Story]) -> tuple[list[Story], list[Story]]:
    """Persist sightings; return (new, updated)."""
    new: list[Story] = []
    updated: list[Story] = []
    for s in stories:
        s.story_id = state.canonical_story_id(s.story_id, s.article_ids)
        if state.upsert_story(s.story_id, s.kind, s.headline, s.source_count, s.raw):
            new.append(s)
        else:
            updated.append(s)
        state.bind_articles(s.story_id, s.article_ids)
    return new, updated
