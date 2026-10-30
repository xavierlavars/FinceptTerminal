"""Minimal Streamable-HTTP MCP client for the terminal's built-in server.

Standard library only. Mirrors scripts/mcp/fincept_mcp_stdio.py: one POST per JSON-RPC
message, bearer auth, JSON or text/event-stream replies. Adds the two conveniences a
pipeline needs: `call()` unwraps tool results into Python objects, and long-running tools
that answer {"job_id", "status": "running"} are polled to completion transparently.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger("headline_algo.client")

PROTOCOL_VERSION = "2025-06-18"


class TerminalError(RuntimeError):
    """A tool call that the terminal refused or that failed."""


class TerminalUnavailable(TerminalError):
    """The terminal is not running or its MCP server is off."""


def _parse_sse(data: bytes) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    chunk: list[str] = []

    def flush() -> None:
        if chunk:
            try:
                messages.append(json.loads("\n".join(chunk)))
            except ValueError:
                pass
            chunk.clear()

    for line in data.decode("utf-8", "replace").splitlines():
        if not line:
            flush()
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            chunk.append(value)
    flush()
    return messages


class TerminalClient:
    def __init__(self, url: str, token: str, timeout: float = 60.0, client_name: str = "headline-algo"):
        self.url = url
        self._token = token
        self.timeout = timeout
        self.client_name = client_name
        self._next_id = 1
        self._session_id = ""
        self._protocol = ""
        self._initialised = False

    # -- transport ----------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + self._token,
            "User-Agent": "headline-algo/0.1",
        }
        if self._protocol:
            h["MCP-Protocol-Version"] = self._protocol
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _post(self, message: dict[str, Any]) -> dict[str, Any] | None:
        raw = json.dumps(message).encode("utf-8")
        req = urllib.request.Request(self.url, data=raw, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
                ctype = resp.headers.get("Content-Type", "") or ""
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self._session_id = sid
                body = resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read() if exc.fp else b""
            detail = body.decode("utf-8", "replace")[:300]
            if exc.code in (401, 403):
                raise TerminalError(f"terminal rejected the token (HTTP {exc.code}): {detail}") from exc
            raise TerminalError(f"terminal returned HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise TerminalUnavailable(f"cannot reach {self.url}: {getattr(exc, 'reason', exc)}") from exc
        if status == 202 or not body.strip():
            return None
        if "text/event-stream" in ctype.lower():
            msgs = _parse_sse(body)
            return msgs[-1] if msgs else None
        return json.loads(body.decode("utf-8"))

    def _request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        msg_id = self._next_id
        self._next_id += 1
        reply = self._post({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})
        if reply is None:
            raise TerminalError(f"no reply to {method}")
        if "error" in reply:
            err = reply["error"]
            raise TerminalError(f"{method}: {err.get('message', err)}")
        return reply.get("result")

    def initialize(self) -> dict[str, Any]:
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": self.client_name, "version": "0.1"},
        })
        self._protocol = str(result.get("protocolVersion") or PROTOCOL_VERSION)
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialised = True
        return result

    # -- tools --------------------------------------------------------------------

    @staticmethod
    def _unwrap(result: dict[str, Any]) -> Any:
        """Tool results arrive as content blocks; the terminal puts JSON in the text block."""
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        text = "\n".join(texts).strip()
        if result.get("isError"):
            raise TerminalError(text or "tool reported an error")
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return text

    def call(self, tool: str, arguments: dict[str, Any] | None = None, *, wait_sec: float = 240.0) -> Any:
        """Call a tool; follow background jobs to completion; return the decoded payload."""
        if not self._initialised:
            self.initialize()
        started = time.monotonic()
        result = self._request("tools/call", {"name": tool, "arguments": arguments or {}})
        payload = self._unwrap(result)
        if isinstance(payload, dict) and payload.get("status") == "running" and payload.get("job_id"):
            payload = self._follow_job(payload["job_id"], tool, wait_sec)
        if isinstance(payload, dict) and payload.get("truncated") and payload.get("result_id"):
            payload = self._fetch_full(str(payload["result_id"]), tool)
        log.debug("%s took %.1fs", tool, time.monotonic() - started)
        return payload

    def _fetch_full(self, result_id: str, tool: str) -> Any:
        """Page a truncated result envelope back together.

        Results over the server's response budget arrive as
        {"data": <partial>, "truncated": true, "result_id": ..., "note": ...}; the full raw
        JSON text is served in byte slices by result_fetch. The reassembled text is itself
        {"data": <full result>}.
        """
        chunks: list[str] = []
        offset = 0
        for _ in range(64):  # 64 x 20 KB is far beyond anything this pipeline asks for
            page = self._unwrap(self._request("tools/call", {
                "name": "result_fetch", "arguments": {"result_id": result_id, "offset": offset, "limit": 20_000},
            }))
            if not isinstance(page, dict):
                raise TerminalError(f"{tool}: result_fetch returned {type(page).__name__}")
            chunks.append(str(page.get("text", "")))
            if not page.get("has_more"):
                break
            offset = int(page.get("next_offset") or (offset + int(page.get("returned") or 0)))
        else:
            raise TerminalError(f"{tool}: result {result_id} too large to page")
        full = json.loads("".join(chunks))
        if isinstance(full, dict) and set(full.keys()) <= {"data"}:
            return full.get("data")
        return full.get("data", full) if isinstance(full, dict) and "data" in full else full

    def _follow_job(self, job_id: str, tool: str, wait_sec: float) -> Any:
        deadline = time.monotonic() + wait_sec
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TerminalError(f"{tool}: job {job_id} still running after {wait_sec:.0f}s")
            status = self._unwrap(self._request("tools/call", {
                "name": "job_status",
                "arguments": {"job_id": job_id, "wait_ms": int(min(30_000, max(1_000, remaining * 1000)))},
            }))
            state = (status or {}).get("status")
            if state in ("succeeded", "failed"):
                break
        result = self._unwrap(self._request("tools/call", {"name": "job_result", "arguments": {"job_id": job_id}}))
        if state == "failed":
            raise TerminalError(f"{tool}: {result}")
        return result

    # -- convenience wrappers used by the pipeline ---------------------------------

    def quote(self, symbol: str) -> dict[str, Any] | None:
        try:
            q = self.call("get_quote", {"symbol": symbol})
        except TerminalError as exc:
            log.info("quote %s unavailable: %s", symbol, exc)
            return None
        return q if isinstance(q, dict) and q.get("price") is not None else None

    def news_clusters(self, time_range: str, min_sources: int, limit: int) -> list[dict[str, Any]]:
        data = self.call("get_news_clusters", {
            "time_range": time_range, "min_sources": min_sources, "limit": limit, "include_articles": True,
        })
        return list((data or {}).get("clusters") or [])

    def threat_alerts(self, time_range: str = "6H", limit: int = 50) -> list[dict[str, Any]]:
        data = self.call("get_threat_alerts", {"time_range": time_range, "limit": limit})
        return list((data or {}).get("alerts") or [])

    def geopolitics_events(self, limit: int = 50) -> list[dict[str, Any]]:
        data = self.call("fetch_geopolitics_events", {"limit": limit})
        return list((data or {}).get("events") or [])

    def lookup_symbol(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        data = self.call("lookup_symbol", {"query": query, "limit": limit})
        if isinstance(data, dict):
            data = data.get("results") or data.get("candidates") or data.get("symbols") or []
        return list(data or [])

    def paper_portfolios(self) -> list[dict[str, Any]]:
        return list(self.call("pt_list_portfolios") or [])

    def paper_positions(self, portfolio_id: str) -> list[dict[str, Any]]:
        data = self.call("pt_get_positions", {"portfolio_id": portfolio_id})
        if isinstance(data, dict):
            data = data.get("positions") or []
        return list(data or [])

    def paper_stats(self, portfolio_id: str) -> dict[str, Any]:
        return dict(self.call("pt_get_stats", {"portfolio_id": portfolio_id}) or {})

    def paper_orders(self, portfolio_id: str, status: str | None = None) -> list[dict[str, Any]]:
        args: dict[str, Any] = {"portfolio_id": portfolio_id}
        if status:
            args["status"] = status
        data = self.call("pt_get_orders", args)
        if isinstance(data, dict):
            data = data.get("orders") or []
        return list(data or [])

    def paper_order(self, portfolio_id: str, symbol: str, side: str, quantity: float, price: float,
                    order_type: str = "market", reduce_only: bool = False) -> dict[str, Any]:
        return dict(self.call("pt_place_order", {
            "portfolio_id": portfolio_id, "symbol": symbol, "side": side, "quantity": quantity,
            "price": price, "order_type": order_type, "reduce_only": reduce_only,
        }) or {})

    def create_note(self, title: str, content: str, *, category: str = "PORTFOLIO",
                    tags: str = "headline-algo", tickers: str = "", priority: str = "LOW") -> Any:
        args: dict[str, Any] = {"title": title, "content": content, "category": category,
                                "tags": tags, "priority": priority}
        if tickers:
            args["tickers"] = tickers
        return self.call("create_note", args)
