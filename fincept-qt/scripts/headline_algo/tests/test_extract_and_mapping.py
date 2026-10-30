from __future__ import annotations

from pathlib import Path

from headline_algo.extract import EVENT_TYPES, Event, rules_extract, validate_event
from headline_algo.ingest import Story, register_stories, story_from_alert, story_from_cluster
from headline_algo.mapping import MacroMap
from headline_algo.state import State

PKG = Path(__file__).resolve().parents[1]


def _story(headline: str, summary: str = "", **kw) -> Story:
    base = dict(story_id="c:x", kind="cluster", headline=headline, summary=summary, category="MARKETS",
                sentiment="NEUTRAL", impact="MEDIUM", priority="ROUTINE", source_count=3, velocity="rising",
                is_breaking=False, tickers=[], headlines=[headline])
    base.update(kw)
    return Story(**base)


def test_rules_extract_classifies_common_headlines():
    cases = {
        "Acme cuts full-year guidance as demand slows": ("guidance_cut", "bearish"),
        "Globex raises outlook after strong quarter": ("guidance_raise", "bullish"),
        "BOJ keeps rates steady, signals further rate hikes": ("central_bank_hawkish", "bearish"),
        "US hits Rwanda with new sanctions": ("sanctions", "bearish"),
        "Binance pauses withdrawals after suspected hack": ("outage_or_cyber", "bearish"),
        "Initech to acquire Vandelay for $2bn": ("m_and_a", "bullish"),
        "Earnings miss sends Alphabet lower": ("earnings_miss", "bearish"),
        "Weather was nice in Cupertino today": ("other", "neutral"),
        # false positives seen in the live journal
        "Netanyahu attacks Israel's Arab parties as he falls short of a majority": ("other", "neutral"),
        "BOJ holds interest rates, raises 2027 inflation outlook": ("other", "neutral"),
        "Newly Freed American Describes His Years in Russian Prison": ("other", "neutral"),
        "Israel launches air strikes on southern Lebanon": ("conflict_escalation", "bearish"),
    }
    for headline, (et, d) in cases.items():
        ev = rules_extract(_story(headline))
        assert (ev.event_type, ev.direction) == (et, d), headline
        assert ev.event_type in EVENT_TYPES


def test_rules_extract_macro_targets_and_confidence():
    ev = rules_extract(_story("Gold falls as US PCE data reinforces Fed hike odds", source_count=4, is_breaking=True))
    assert "gold" in ev.macro_targets and "us_treasuries" in ev.macro_targets
    assert ev.event_type == "central_bank_hawkish"
    assert 0.6 <= ev.confidence <= 0.8
    assert rules_extract(_story("Nothing happened")).confidence < 0.5


def test_validate_event_rejects_garbage_and_normalises():
    assert validate_event({"event_type": "moon_landing", "direction": "bullish"}, "x") is None
    assert validate_event("not a dict", "x") is None
    ev = validate_event({"event_type": "Sanctions", "direction": "BEARISH", "confidence": "1.7",
                         "tickers": ["aapl", "AAPL"], "macro_targets": ["oil", "bogus"], "companies": ["Acme"]}, "llm")
    assert ev is not None
    assert ev.event_type == "sanctions" and ev.direction == "bearish" and ev.confidence == 1.0
    assert ev.tickers == ["AAPL"] and ev.macro_targets == ["oil"] and ev.backend == "llm"


def test_cluster_normalisation_drops_junk_tickers():
    cluster = {
        "id": "abc", "category": "ECONOMIC", "sentiment": "BEARISH", "source_count": 2, "velocity": "rising",
        "is_breaking": True,
        "lead": {"id": "n1", "headline": "Hot CPI lifts RBA hike bets", "summary": "", "impact": "LOW",
                 "priority": "ROUTINE", "tickers": ["RBA", "PCE", "CBA"], "source": "FXSTREET"},
        "articles": [
            {"id": "n1", "headline": "Hot CPI lifts RBA hike bets", "summary": "longer summary here", "impact": "HIGH",
             "priority": "FLASH", "tickers": ["RBA", "CBA"], "source": "FXSTREET", "link": "u1"},
            {"id": "n2", "headline": "Aussie rises on CPI", "summary": "", "impact": "LOW", "priority": "ROUTINE",
             "tickers": ["IT"], "source": "REUTERS", "link": "u2"},
        ],
    }
    s = story_from_cluster(cluster)
    assert s.story_id == "c:n1" and s.article_ids == ["n1", "n2"]
    assert s.tickers == ["CBA"] and s.impact == "HIGH" and s.priority == "FLASH"
    assert s.summary == "longer summary here" and s.sources == ["FXSTREET", "REUTERS"]
    a = story_from_alert({"id": "n1", "headline": "h", "threat_category": "cyber", "threat_confidence": 0.8,
                          "tickers": ["EV"], "source": "X"})
    assert a.kind == "alert" and a.tickers == [] and a.threat_confidence == 0.8


def test_macro_map_overrides_win():
    mm = MacroMap(PKG / "macro_map.json")
    ev = Event(event_type="conflict_escalation", direction="bearish", confidence=0.7, macro_targets=["us_equities"])
    syms = {c.symbol: c.why for c in mm.instruments(ev)}
    # escalation: bearish us_equities has no instrument, but defense/gold/oil are bought
    assert {"ITA", "GLD", "USO"} <= set(syms)
    assert "SPY" not in syms
    ev2 = Event(event_type="macro_data_strong", direction="bullish", confidence=0.7, macro_targets=["us_equities"])
    assert [c.symbol for c in mm.instruments(ev2)] == ["SPY"]


def test_story_identity_survives_cluster_id_and_lead_changes():
    state = State(":memory:")
    arts = [{"id": "n1", "headline": "A", "source": "X"}, {"id": "n2", "headline": "B", "source": "Y"}]
    c1 = {"id": "k1", "lead": arts[0], "articles": arts, "source_count": 2}
    s1 = story_from_cluster(c1)
    new, upd = register_stories(state, [s1])
    assert [x.story_id for x in new] == ["c:n1"] and not upd
    # next poll: new cluster id, new lead, one extra article
    arts2 = [{"id": "n3", "headline": "C", "source": "Z"}] + arts
    c2 = {"id": "k9", "lead": arts2[0], "articles": arts2, "source_count": 3}
    s2 = story_from_cluster(c2)
    new, upd = register_stories(state, [s2])
    assert not new and [x.story_id for x in upd] == ["c:n1"]
    # an alert for an article already in the cluster is the same story
    a = story_from_alert({"id": "n2", "headline": "B", "source": "Y"})
    new, upd = register_stories(state, [a])
    assert not new and upd[0].story_id == "c:n1"
