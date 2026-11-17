import json
import urllib.parse

import httpx
import pytest
from fastapi.testclient import TestClient

from dikigoros.config import Settings
from dikigoros.lawapi import LawAPI, LawAPIError
from dikigoros.server import create_app

from conftest import ScriptedLLM


def client_for(settings, **kw) -> TestClient:
    return TestClient(create_app(settings, **kw))


def events_of(response) -> list[dict]:
    return [json.loads(line[6:]) for line in response.text.split("\n") if line.startswith("data: ")]


def test_meta_health_and_frontend(settings):
    with client_for(settings) as c:
        meta = c.get("/api/meta").json()
        assert meta["llm"] == "fake" and meta["auth_required"] is False
        assert [a["short"] for a in meta["catalogue"]] == ["Σ", "ΑΚ", "ΚΑΕΔ"]
        assert meta["index"]["by_kind"]["law"] == 2
        assert c.get("/health").json()["ok"] is True
        assert "Ο Δικηγόρος σου" in c.get("/").text
        assert c.get("/static/app.js").status_code == 200


def test_ask_streams_events_in_order(settings):
    llm = ScriptedLLM(plan={"searches": [{"q": "καταγγελία σύμβασης εργασίας προειδοποίηση"}]},
                      answer="Απαιτείται έγγραφη προειδοποίηση [1].")
    with client_for(settings, llm=llm) as c:
        r = c.post("/api/ask", json={"question": "Πότε απαιτείται προειδοποίηση;", "kinds": ["code"]})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        events = events_of(r)
        assert events[0]["type"] == "status" and events[-1]["type"] == "done"
        assert [s["cite"] for s in next(e for e in events if e["type"] == "sources")["sources"]] == ["ΚΑΕΔ 321"]
        assert "".join(e["delta"] for e in events if e["type"] == "text") == "Απαιτείται έγγραφη προειδοποίηση [1]."


@pytest.mark.parametrize("body,status", [
    ({"question": "   "}, 422), ({"question": "ε" * 7000}, 422), ({}, 422),
    ({"question": "ε", "as_of": "χθες"}, 422), ({"question": "ε", "history": [{"role": "user"}] * 41}, 422),
])
def test_ask_rejects_bad_input(settings, body, status):
    with client_for(settings) as c:
        r = c.post("/api/ask", json=body)
        assert r.status_code == status


def test_ask_needs_a_model(settings, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.test/v1")
    with client_for(Settings()) as c:
        assert c.get("/api/meta").json()["llm"] == "off"
        r = c.post("/api/ask", json={"question": "ερώτηση"})
        assert r.status_code == 503 and "LLM_API_KEY" in r.json()["error"]
        assert c.get("/api/search", params={"q": "ηθική βλάβη"}).status_code == 200


def test_access_code_guards_the_api(settings, monkeypatch):
    monkeypatch.setenv("ACCESS_CODE", "μυστικό-123")
    with client_for(Settings()) as c:
        assert c.get("/api/meta").json()["auth_required"] is True
        assert c.get("/api/search", params={"q": "ηθική βλάβη"}).status_code == 401
        assert c.post("/api/ask", json={"question": "ε"}, headers={"X-Access-Code": "wrong"}).status_code == 401
        good = {"X-Access-Code": urllib.parse.quote("μυστικό-123")}
        assert c.post("/api/login", headers=good).status_code == 200
        assert c.get("/api/search", params={"q": "ηθική βλάβη"}, headers=good).status_code == 200


def test_rate_limit_per_address(settings, monkeypatch):
    monkeypatch.setenv("ASK_PER_HOUR", "2")
    with client_for(Settings()) as c:
        codes = [c.post("/api/ask", json={"question": "ηθική βλάβη"}, headers={"Fly-Client-IP": "1.1.1.1"}).status_code
                 for _ in range(3)]
        assert codes == [200, 200, 429]
        assert c.post("/api/ask", json={"question": "ηθική βλάβη"}, headers={"Fly-Client-IP": "2.2.2.2"}).status_code == 200


def test_search_unit_and_act_endpoints(settings):
    with client_for(settings) as c:
        hits = c.get("/api/search", params={"q": "ηθικής βλάβης", "kinds": "code"}).json()["hits"]
        assert hits[0]["cite"] == "ΑΚ 932" and "text" not in hits[0]
        unit = c.get("/api/unit", params={"uid": "codes/civil-code/art-914"}).json()
        assert unit["text"].startswith("Όποιος ζημιώσει") and unit["next"]["label"] == "Άρθρο 932"
        assert unit["live_available"] is True
        assert c.get("/api/unit", params={"uid": "laws/2019/4648/art-1"}).json()["live_available"] is False
        assert c.get("/api/unit", params={"uid": "δεν/υπάρχει"}).status_code == 404
        act = c.get("/api/act", params={"id": "ΑΚ"}).json()
        assert act["act"]["short"] == "ΑΚ" and len(act["toc"]) == 3
        assert c.get("/api/search", params={"q": "α"}).status_code == 422


def law_api(settings, handler) -> LawAPI:
    return LawAPI(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_live_status_and_its_failures(settings):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={"tool": "read_law", "tree": "abc", "data": {
            "in_force": True, "summary": "σε ισχύ", "changes_count": 1,
            "last_change": {"law": "π.δ. 456/1984", "in_force": "1984-10-24"},
            "history": [{"law": "π.δ. 456/1984", "in_force": "1984-10-24"}],
            "cite": {"official_url": "https://api.et.gr/x", "pincite": "Άρθρο 914"}, "latest": {"summary": "ίδιο"}}})

    with client_for(settings, law=law_api(settings, handler)) as c:
        live = c.get("/api/live", params={"uid": "codes/civil-code/art-914"}).json()
        assert live["in_force"] is True and live["last_change"]["law"] == "π.δ. 456/1984"
        assert calls == [("https://global-law.lexifina.com/v1/read_law",
                          {"path": "greece/national/acts/codes/civil-code/art-914.md", "include_text": False})]
        c.get("/api/live", params={"uid": "codes/civil-code/art-914"})
        assert len(calls) == 1                                         # δεύτερη φορά από τη μνήμη
        assert c.get("/api/live", params={"uid": "laws/2019/4648/art-1"}).status_code == 422

    with client_for(settings, law=law_api(settings, lambda r: httpx.Response(429, json={"error": "limit"}))) as c:
        r = c.get("/api/live", params={"uid": "codes/civil-code/art-914"})
        assert r.status_code == 429 and "όριο" in r.json()["error"]


async def test_law_api_reads_several_provisions_as_of_a_date(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"paths": ["a.md", "b.md"], "date": "2020-01-01", "max_chars": 100000}
        return httpx.Response(200, json={"data": {
            "provisions": [{"path": "a.md", "in_force": True, "last_change": {"law": "ν. 1/2019", "in_force": "2019-07-01"}},
                           {"path": "b.md", "in_force": False, "last_change": None}],
            "text": "=== a.md (Last changed by ν. 1/2019, in force 2019-07-01) ===\n# Άρθρο 1\n\nΚείμενο α.\n"}})

    api = law_api(settings, handler)
    out = await api.read_as_of(["a.md", "b.md", "a.md"], "2020-01-01")
    assert out["a.md"] == {"in_force": True, "text": "# Άρθρο 1\n\nΚείμενο α.", "law": "ν. 1/2019", "since": "2019-07-01"}
    assert out["b.md"]["in_force"] is False and out["b.md"]["text"] == ""
    assert api.budget()["left_day"] == 99


async def test_law_api_stops_at_its_daily_budget(settings, monkeypatch):
    monkeypatch.setenv("LAW_API_PER_DAY", "1")
    monkeypatch.setenv("LAW_API_PER_MINUTE", "5")
    api = law_api(Settings(), lambda r: httpx.Response(200, json={"data": {"in_force": True}}))
    await api.status("a.md")
    with pytest.raises(LawAPIError) as err:
        await api.status("b.md")
    assert err.value.limited
