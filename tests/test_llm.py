import json

import httpx
import pytest

from dikigoros.config import Settings
from dikigoros.llm import LLM, LLMError, ThinkFilter, parse_json


def test_parse_json_is_tolerant():
    assert parse_json('{"a": 1}') == {"a": 1}
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('<think>σκέψη {x}</think>Ορίστε: {"a": [1, 2]} τέλος') == {"a": [1, 2]}
    for bad in ("", "καμία δομή", "[1, 2]", '{"a": '):
        with pytest.raises(LLMError):
            parse_json(bad)


def test_think_filter_across_chunk_boundaries():
    f = ThinkFilter()
    out = "".join(f.feed(c) for c in ["Κα", "λη<th", "ink>κρυ", "φό</thi", "nk>μέρα <", "b>"]) + f.flush()
    assert out == "Καλημέρα <b>"
    f = ThinkFilter()
    assert f.feed("α<think>ατελείωτο") + f.flush() == "α"


def make_llm(handler, monkeypatch) -> LLM:
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_ROUTER_EXTRA_BODY", '{"reasoning_effort": "low"}')
    return LLM(Settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_complete_json_sends_extra_body_and_drops_unsupported_json_mode(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        assert request.url == "https://llm.test/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer k"
        if "response_format" in body:
            return httpx.Response(400, json={"error": "response_format not supported"})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    llm = make_llm(handler, monkeypatch)
    assert await llm.complete_json("σύστημα", "χρήστης") == {"ok": True}
    assert len(seen) == 2 and seen[0]["reasoning_effort"] == "low" and "response_format" not in seen[1]


async def test_complete_json_reports_provider_errors(monkeypatch):
    llm = make_llm(lambda r: httpx.Response(401, json={"error": "bad key"}), monkeypatch)
    with pytest.raises(LLMError, match="401"):
        await llm.complete_json("s", "u")


async def test_stream_yields_content_only(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        chunks = [{"choices": [{"delta": {"reasoning_content": "σκέφτομαι"}}]},
                  {"choices": [{"delta": {"content": "<think>κρυφό</think>Κατά το "}}]},
                  {"choices": []},
                  {"choices": [{"delta": {"content": "άρθρο 914 [1]."}}]}]
        text = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})

    llm = make_llm(handler, monkeypatch)
    assert "".join([p async for p in llm.stream([{"role": "user", "content": "q"}])]) == "Κατά το άρθρο 914 [1]."


async def test_stream_error_status(monkeypatch):
    llm = make_llm(lambda r: httpx.Response(403, text="forbidden"), monkeypatch)
    with pytest.raises(LLMError, match="403"):
        [p async for p in llm.stream([{"role": "user", "content": "q"}])]
