from __future__ import annotations

import json

import pytest

from headline_algo.terminal_client import TerminalClient, TerminalError, _parse_sse


def test_parse_sse_extracts_data_lines():
    body = b": keepalive\n\nevent: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"ok\":true}}\n\n"
    msgs = _parse_sse(body)
    assert msgs == [{"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}]


def test_unwrap_prefers_structured_then_json_text():
    assert TerminalClient._unwrap({"structuredContent": {"a": 1}}) == {"a": 1}
    text = json.dumps({"price": 10})
    assert TerminalClient._unwrap({"content": [{"type": "text", "text": text}]}) == {"price": 10}
    assert TerminalClient._unwrap({"content": [{"type": "text", "text": "plain"}]}) == "plain"
    with pytest.raises(TerminalError):
        TerminalClient._unwrap({"isError": True, "content": [{"type": "text", "text": "nope"}]})


def test_call_follows_background_jobs(monkeypatch):
    c = TerminalClient("http://127.0.0.1:1/mcp", "t")
    c._initialised = True
    calls = []

    def fake_request(method, params=None):
        calls.append((method, params["name"]))
        name = params["name"]
        if name == "slow_tool":
            return {"content": [{"type": "text", "text": json.dumps({"job_id": "j1", "status": "running"})}]}
        if name == "job_status":
            return {"content": [{"type": "text", "text": json.dumps({"job_id": "j1", "status": "succeeded"})}]}
        if name == "job_result":
            return {"content": [{"type": "text", "text": json.dumps({"answer": 42})}]}
        raise AssertionError(name)

    monkeypatch.setattr(c, "_request", fake_request)
    assert c.call("slow_tool", {}) == {"answer": 42}
    assert [n for _, n in calls] == ["slow_tool", "job_status", "job_result"]


def test_call_pages_truncated_envelopes(monkeypatch):
    c = TerminalClient("http://127.0.0.1:1/mcp", "t")
    c._initialised = True
    full = json.dumps({"data": {"clusters": [{"id": str(i)} for i in range(50)], "count": 50}})
    half = len(full) // 2
    seen = []

    def fake_request(method, params=None):
        name = params["name"]
        args = params.get("arguments", {})
        seen.append((name, args.get("offset")))
        if name == "big_tool":
            env = {"data": {"clusters": [{"id": "0"}], "count": 50}, "truncated": True, "result_id": "res_1",
                   "note": "page it"}
            return {"content": [{"type": "text", "text": json.dumps(env)}]}
        if name == "result_fetch":
            off = args["offset"]
            chunk = full[off:off + half]
            nxt = off + len(chunk)
            return {"content": [{"type": "text", "text": json.dumps(
                {"text": chunk, "has_more": nxt < len(full), "next_offset": nxt, "returned": len(chunk)})}]}
        raise AssertionError(name)

    monkeypatch.setattr(c, "_request", fake_request)
    out = c.call("big_tool", {})
    assert out["count"] == 50 and len(out["clusters"]) == 50
    pages = -(-len(full) // half)  # ceil
    assert [n for n, _ in seen] == ["big_tool"] + ["result_fetch"] * pages
    assert [o for n, o in seen if n == "result_fetch"] == [i * half for i in range(pages)]
