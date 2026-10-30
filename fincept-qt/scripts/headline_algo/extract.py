"""Event extraction: a Story becomes a fixed-schema Event.

Fixed categories matter because you can only evaluate what you can count. Three
backends share one schema:

  rules      - deterministic, uses the terminal's own tags plus headline keywords.
               Lower confidence, zero cost, always available. Also the fallback.
  claude     - `claude -p` (Claude Code CLI) on the user's subscription. Needs `claude login`.
  anthropic  - direct Messages API with ANTHROPIC_API_KEY.

LLM output is validated strictly; anything malformed falls back to the rules result
and is marked as such, so the journal always says which backend produced an event.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import ExtractConfig
from .ingest import Story

log = logging.getLogger("headline_algo.extract")

EVENT_TYPES = [
    "earnings_beat", "earnings_miss", "guidance_raise", "guidance_cut", "m_and_a",
    "regulatory_action", "sanctions", "central_bank_hawkish", "central_bank_dovish",
    "macro_data_strong", "macro_data_weak", "outage_or_cyber", "conflict_escalation",
    "conflict_deescalation", "product_or_contract", "management_change", "other",
]
DIRECTIONS = ["bullish", "bearish", "neutral"]
# Macro targets the mapping layer knows how to turn into US-listed ETFs (see macro_map.json).
MACRO_TARGETS = [
    "us_equities", "us_treasuries", "usd", "yen", "euro", "gold", "oil", "natural_gas",
    "crypto_btc", "semis", "banks", "energy", "defense", "airlines", "homebuilders",
    "china_equities", "japan_equities", "europe_equities", "india_equities", "emerging_markets",
    "volatility",
]


@dataclass
class Event:
    event_type: str
    direction: str
    confidence: float
    companies: list[str] = field(default_factory=list)   # names as written in the story
    tickers: list[str] = field(default_factory=list)     # explicit tickers if the story gives them
    macro_targets: list[str] = field(default_factory=list)
    rationale: str = ""
    backend: str = "rules"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clamp(x: Any, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return 0.0


def validate_event(data: Any, backend: str) -> Event | None:
    if not isinstance(data, dict):
        return None
    et = str(data.get("event_type", "")).strip().lower()
    d = str(data.get("direction", "")).strip().lower()
    if et not in EVENT_TYPES or d not in DIRECTIONS:
        return None
    def _strs(key: str, allowed: list[str] | None = None) -> list[str]:
        out: list[str] = []
        for v in data.get(key) or []:
            s = str(v).strip()
            if not s:
                continue
            if allowed is not None:
                s = s.lower()
                if s not in allowed:
                    continue
            else:
                s = s.upper() if key == "tickers" else s
            if s not in out:
                out.append(s)
        return out[:8]
    return Event(
        event_type=et, direction=d, confidence=_clamp(data.get("confidence")),
        companies=_strs("companies"), tickers=_strs("tickers"),
        macro_targets=_strs("macro_targets", MACRO_TARGETS),
        rationale=str(data.get("rationale") or "")[:400], backend=backend,
    )


# -- rules backend ------------------------------------------------------------------

_KW = [
    # (regex, event_type, direction)
    (r"\b(beats?|tops?|exceeds?)\b.*\b(estimates?|expectations|forecasts?)\b", "earnings_beat", "bullish"),
    (r"\b(miss(es|ed)?|falls? short|disappoint)\b.*\b(estimates?|expectations|forecasts?)\b|\bearnings miss\b",
     "earnings_miss", "bearish"),
    (r"\b(rais(es|ed)|lifts?|hikes?)\b.*\b(guidance|outlook|forecast)\b", "guidance_raise", "bullish"),
    (r"\b(cuts?|lowers?|slash(es|ed)|trims?)\b.*\b(guidance|outlook|forecast)\b", "guidance_cut", "bearish"),
    (r"\b(to acquire|acquires|acquisition of|merger|buyout|takeover|to buy)\b", "m_and_a", "bullish"),
    (r"\bsanction", "sanctions", "bearish"),
    (r"\b(antitrust|regulator|lawsuit|probe|investigation|fine[sd]?|ban(s|ned)?|recall)\b",
     "regulatory_action", "bearish"),
    (r"\b(rate[- ]hikes?|hike (odds|bets|expectations)|raises? (interest )?rates?|hawkish|further (rate )?hikes?)\b",
     "central_bank_hawkish", "bearish"),
    (r"\b(rate[- ]cuts?|cut (odds|bets|expectations)|cuts? (interest )?rates?|dovish|easing)\b",
     "central_bank_dovish", "bullish"),
    (r"\b(outage|hack(ed|ers)?|cyberattack|breach|ransomware|paus(es|ed) withdrawals)\b",
     "outage_or_cyber", "bearish"),
    (r"\b(air ?strikes?|missile|invasion|invades?|escalat\w*|shelling|offensive|troops|"
     r"(drone|missile|rocket|military|terror(ist)?) attacks?|attack on)\b",
     "conflict_escalation", "bearish"),
    (r"\b(ceasefire|truce|peace (deal|agreement|talks)|de-?escalat)\b", "conflict_deescalation", "bullish"),
    (r"\b(wins? (a )?contract|awarded|order (worth|valued)|launch(es|ed)?|unveils?)\b",
     "product_or_contract", "bullish"),
    (r"\b(ceo|cfo|chief executive)\b.*\b(steps down|resigns?|departs?|ousted|named|appointed)\b",
     "management_change", "neutral"),
]
_MACRO_KW = [
    (r"\b(fed|fomc|federal reserve|treasur(y|ies)|pce|cpi|payrolls|gdp)\b", ["us_treasuries", "usd"]),
    (r"\b(boj|bank of japan|yen)\b", ["yen", "japan_equities"]),
    (r"\b(ecb|european central bank|euro\b|eurozone)\b", ["euro", "europe_equities"]),
    (r"\b(gold|bullion|xau)\b", ["gold"]),
    (r"\b(crude|oil prices?|opec|brent|wti|tanker)\b", ["oil", "energy"]),
    (r"\b(natural gas|lng)\b", ["natural_gas"]),
    (r"\b(bitcoin|btc|crypto|binance|coinbase)\b", ["crypto_btc"]),
    (r"\b(chip|semiconductor|nvidia|tsmc|asml)\b", ["semis"]),
    (r"\b(pentagon|defen[cs]e|missile|nato)\b", ["defense"]),
    (r"\b(china|beijing|pla\b|yuan)\b", ["china_equities"]),
    (r"\b(india|rbi|sensex|nifty)\b", ["india_equities"]),
    (r"\b(airline|boeing|airbus)\b", ["airlines"]),
    (r"\b(bank(s|ing)? (crisis|failure|run)|lender)\b", ["banks"]),
]


RULES_VERSION = 2
RULES_TAG = f"rules/v{RULES_VERSION}"
# Central-bank stories are about rates, not corporate guidance: "BOJ raises inflation
# outlook" must not become guidance_raise. When a central bank is the subject, only the
# central-bank rules may fire; a plain hold/no-change is "other".
_CB_RX = re.compile(r"\b(fed|fomc|federal reserve|boj|bank of japan|ecb|european central bank|boe|bank of england|"
                    r"rba|reserve bank|rbi|snb|boc|bank of canada|pboc|central bank|interest rates?|rate decision)\b")
_CB_RULES = [r for r in _KW if r[1] in ("central_bank_hawkish", "central_bank_dovish")]


def rules_extract(story: Story) -> Event:
    # Event keywords are matched on headlines only. Summaries carry background ("...after
    # Russia's invasion in 2022") that turns human-interest pieces into "conflict escalation".
    head_text = (story.headline + " | " + " | ".join(story.headlines[:5])).lower()
    text = (head_text + " " + story.summary).lower()
    et, d = "other", "neutral"
    rules = _CB_RULES if _CB_RX.search(head_text) else _KW
    for rx, kind, direction in rules:
        if re.search(rx, head_text):
            et, d = kind, direction
            break
    # Terminal sentiment refines direction when the keyword rule is neutral or absent.
    ts = story.sentiment.upper()
    if et == "other" and ts in ("BULLISH", "BEARISH"):
        d = ts.lower()
    macro: list[str] = []
    for rx, targets in _MACRO_KW:
        if re.search(rx, text):
            for t in targets:
                if t not in macro:
                    macro.append(t)
    # Central-bank events carry direction for the currency, not equities; keep as given.
    conf = 0.35
    if et != "other":
        conf = 0.55
        if story.source_count >= 3:
            conf += 0.1
        if story.is_breaking or story.priority in ("FLASH", "URGENT", "BREAKING"):
            conf += 0.05
        if story.impact == "HIGH":
            conf += 0.05
    if story.kind == "alert" and story.threat_confidence:
        conf = max(conf, min(0.65, story.threat_confidence - 0.1))
    return Event(event_type=et, direction=d, confidence=round(min(conf, 0.8), 2), companies=[],
                 tickers=list(story.tickers), macro_targets=macro,
                 rationale="rules: keyword/tag match", backend=RULES_TAG)


# -- LLM backends ----------------------------------------------------------------------

PROMPT = """You classify a financial news story into a strict JSON schema. Output ONLY the JSON object.

Schema:
{{
  "event_type": one of {event_types},
  "direction": "bullish" | "bearish" | "neutral"   (for the most directly affected listed company or asset),
  "confidence": 0.0-1.0   (how sure you are the event is real, material, and correctly classified),
  "companies": [up to 5 company names directly affected, as written],
  "tickers": [US-listed tickers you are certain of, else empty],
  "macro_targets": subset of {macro_targets},
  "rationale": one sentence
}}

Rules: use "other" when nothing fits; never invent tickers; confidence <= 0.5 for single-source
or vague stories; direction is about price impact, not whether the news is good for the world.

STORY:
{story}
"""


def _extract_json(text: str) -> Any:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in LLM output")
    return json.loads(text[start:end + 1])


def claude_cli_extract(story: Story, cfg: ExtractConfig) -> Event | None:
    prompt = PROMPT.format(event_types=json.dumps(EVENT_TYPES), macro_targets=json.dumps(MACRO_TARGETS),
                           story=story.text())
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json", "--model", cfg.model],
            capture_output=True, text=True, timeout=cfg.timeout_sec, env=env, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("claude CLI failed: %s", exc)
        return None
    try:
        outer = json.loads(proc.stdout)
        if outer.get("is_error"):
            log.warning("claude CLI error: %s", str(outer.get("result"))[:200])
            return None
        return validate_event(_extract_json(str(outer.get("result", ""))), "claude")
    except ValueError as exc:
        log.warning("claude CLI output unusable: %s", exc)
        return None


def anthropic_extract(story: Story, cfg: ExtractConfig) -> Event | None:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        log.warning("ANTHROPIC_API_KEY not set")
        return None
    prompt = PROMPT.format(event_types=json.dumps(EVENT_TYPES), macro_targets=json.dumps(MACRO_TARGETS),
                           story=story.text())
    body = json.dumps({"model": cfg.anthropic_model, "max_tokens": 400,
                       "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"},
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout_sec) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("anthropic API failed: %s", exc)
        return None
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    try:
        return validate_event(_extract_json(text), "anthropic")
    except ValueError as exc:
        log.warning("anthropic output unusable: %s", exc)
        return None


def extract(story: Story, cfg: ExtractConfig) -> Event:
    base = rules_extract(story)
    if cfg.backend == "rules" or story.source_count < cfg.llm_min_sources and story.kind == "cluster":
        return base
    llm: Event | None = None
    if cfg.backend == "claude":
        llm = claude_cli_extract(story, cfg)
    elif cfg.backend == "anthropic":
        llm = anthropic_extract(story, cfg)
    if llm is None:
        base.rationale += " (LLM unavailable, rules fallback)"
        return base
    # Keep terminal-tagged tickers the model dropped; the mapper validates them anyway.
    for t in story.tickers:
        if t not in llm.tickers:
            llm.tickers.append(t)
    return llm
