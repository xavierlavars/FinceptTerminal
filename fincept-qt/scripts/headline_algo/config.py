"""Configuration: defaults, JSON overrides, and MCP token resolution."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_URL = "http://127.0.0.1:41720/mcp"
TOKEN_ENV = "FINCEPT_MCP_TOKEN"
PKG_DIR = Path(__file__).resolve().parent


@dataclass
class RiskConfig:
    # Mirrors the terminal's `risk.*` settings; the terminal skips those for the local
    # paper simulator, so the pipeline enforces its own copy here.
    max_order_value: float = 10_000.0
    max_order_quantity: int = 1_000
    max_open_positions: int = 8
    daily_loss_limit: float = 2_000.0
    per_symbol_cooldown_min: int = 240


@dataclass
class SignalConfig:
    min_sources: int = 2
    min_confidence: float = 0.6
    # Theme-only trades (an ETF chosen from macro_map, no company named) need more conviction:
    # they are the easiest for a keyword rule to get wrong.
    min_confidence_macro: float = 0.7
    # Skip when the price has already moved more than this (percent) since the
    # story was first seen. This one rule removes most "last one in" trades.
    max_stale_move_pct: float = 1.5
    # Event types the rule layer is allowed to act on. Everything else is logged only.
    tradeable_events: list[str] = field(default_factory=lambda: [
        "earnings_beat", "earnings_miss", "guidance_raise", "guidance_cut",
        "regulatory_action", "sanctions", "m_and_a", "product_or_contract",
        "central_bank_hawkish", "central_bank_dovish", "macro_data_strong", "macro_data_weak",
        "outage_or_cyber", "conflict_escalation", "conflict_deescalation",
    ])
    # Long-only in phase one: bearish events close an existing long or map to a
    # defensive ETF via macro_map; they never open shorts.
    long_only: bool = True


@dataclass
class ExitConfig:
    stop_loss_pct: float = 2.0
    take_profit_pct: float = 4.0
    max_hold_hours: float = 48.0


@dataclass
class ExtractConfig:
    # "rules" needs nothing. "claude" shells out to `claude -p` (run `claude login` once).
    # "anthropic" uses ANTHROPIC_API_KEY. Unknown/failed LLM output falls back to rules.
    backend: str = "rules"
    model: str = "sonnet"
    anthropic_model: str = "claude-sonnet-5-5"
    timeout_sec: int = 90
    # Only send clusters past the cheap filters to the LLM.
    llm_min_sources: int = 2


@dataclass
class Config:
    terminal_url: str = DEFAULT_URL
    token_file: str = ""
    portfolio_id: str = ""
    portfolio_name: str = "Systematic Paper"
    poll_interval_sec: int = 30
    cluster_time_range: str = "6H"
    cluster_limit: int = 40
    # Fincept's geopolitics feed is credit-metered (3 credits per call). Off by default.
    use_geopolitics_feed: bool = False
    # US-listed only in phase one: the paper engine fills on yfinance ticks and the
    # market-hours rule assumes the US regular session.
    market_tz: str = "America/New_York"
    session_open: str = "09:30"
    session_close: str = "16:00"
    skip_first_minutes: int = 5
    skip_last_minutes: int = 10
    state_db: str = str(Path.home() / "Library/Application Support/com.fincept.terminal/headline_algo.db")
    macro_map_file: str = str(PKG_DIR / "macro_map.json")
    daily_note: bool = True
    risk: RiskConfig = field(default_factory=RiskConfig)
    signal: SignalConfig = field(default_factory=SignalConfig)
    exit: ExitConfig = field(default_factory=ExitConfig)
    extract: ExtractConfig = field(default_factory=ExtractConfig)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _merge(dc: Any, data: dict[str, Any]) -> None:
    for key, value in data.items():
        if not hasattr(dc, key):
            raise KeyError(f"unknown config key: {key}")
        current = getattr(dc, key)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            _merge(current, value)
        else:
            setattr(dc, key, value)


def load_config(path: str | os.PathLike[str] | None) -> Config:
    cfg = Config()
    if path:
        with open(path, encoding="utf-8") as fh:
            _merge(cfg, json.load(fh))
    env_url = os.environ.get("FINCEPT_MCP_URL")
    if env_url:
        cfg.terminal_url = env_url
    return cfg


def resolve_token(cfg: Config) -> str:
    """Bearer token for the terminal's MCP server.

    Order: FINCEPT_MCP_TOKEN env var, config token_file, then the entry Claude Code's
    "ADD TO CLAUDE CODE" button wrote to ~/.claude.json. The token never gets logged.
    """
    tok = os.environ.get(TOKEN_ENV, "").strip()
    if tok:
        return tok
    if cfg.token_file:
        tok = Path(cfg.token_file).expanduser().read_text(encoding="utf-8").strip()
        if tok:
            return tok
    claude_cfg = Path.home() / ".claude.json"
    if claude_cfg.exists():
        try:
            data = json.loads(claude_cfg.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
        servers = data.get("mcpServers") or {}
        for name in ("fincept_terminal", "fincept", "fincept-terminal"):
            entry = servers.get(name) or {}
            auth = (entry.get("headers") or {}).get("Authorization", "")
            if auth.lower().startswith("bearer "):
                return auth[7:].strip()
    raise RuntimeError(
        "No MCP token: set FINCEPT_MCP_TOKEN, or token_file in the config, or click "
        "ADD TO CLAUDE CODE in Agent Workbench > CONNECT."
    )
