from dikigoros.lawapi import LawAPIError
from dikigoros.llm import LLMError
from dikigoros.pipeline import (NOTHING_FOUND, OFF_TOPIC, Options, Pipeline, check_citations,
                                extract_citations, history_messages, retrieve)

from conftest import ScriptedLLM


async def collect(pipeline, question, **kw):
    events = [ev async for ev in pipeline.run(question, **kw)]
    text = "".join(ev["delta"] for ev in events if ev["type"] == "text")
    return events, text


def one(events, kind):
    return next(ev for ev in events if ev["type"] == kind)


def test_extract_citations():
    text = "κατά το 914 ΑΚ [3] και [3, παρ. 2][5], ενώ [1, 2] αλλά [sic] και [βλ. 4] [12 › παρ. 1 εδ. β]"
    assert extract_citations(text) == [(3, ""), (3, "παρ. 2"), (5, ""), (1, ""), (2, ""), (12, "παρ. 1 εδ. β")]


def test_check_citations_flags_invalid_and_unverified_paragraphs():
    sources = [{"n": 1, "text": "1. Πρώτη παράγραφος.\n2. Δεύτερη.", "attached": True},
               {"n": 2, "text": "Χωρίς αρίθμηση.", "attached": True},
               {"n": 3, "text": "Δεν δόθηκε.", "attached": False}]
    check = check_citations("Α [1, παρ. 2]. Β [1, παρ. 7]. Γ [3]. Δ [9].", sources)
    assert check["cited"] == [1] and check["invalid"] == [3, 9] and check["uncited"] == [2]
    assert check["unverified_pins"] == [{"n": 1, "pin": "παρ. 7"}]
    assert check_citations("μακρύ κείμενο χωρίς παραπομπές " * 20, sources)["no_citations"] is True


def test_history_messages_start_with_user_and_are_trimmed():
    history = [{"role": "assistant", "content": "ορφανό"}, {"role": "user", "content": "ε" * 5000},
               {"role": "assistant", "content": "α"}, {"role": "system", "content": "αγνοείται"}]
    out = history_messages(history, turns=4)
    assert [m["role"] for m in out] == ["user", "assistant"] and len(out[0]["content"]) < 1600


def test_retrieve_merges_searches_direct_citations_and_previous_sources(store):
    plan = {"question": "ηθική βλάβη", "searches": [{"q": "χρηματική ικανοποίηση ηθική βλάβη", "kinds": [], "acts": []}],
            "citations": [{"act": "Α.Κ.", "article": "914"}, {"act": "ν. 1/1900", "article": "1"}]}
    found = retrieve(store, plan, Options(), ["constitution/art-014"], limit=10)
    cites = [u["cite"] for u in found]
    assert cites[0] == "ΑΚ 914" and found[0]["via"] == ["παραπομπή"]   # η άμεση παραπομπή προηγείται
    assert "ΑΚ 932" in cites and "Σ 14" in cites
    assert next(u for u in found if u["cite"] == "Σ 14")["via"] == ["προηγούμενη απάντηση"]


def test_retrieve_respects_user_filters(store):
    plan = {"question": "καταγγελία σύμβασης εργασίας", "citations": [{"act": "ΑΚ", "article": "914"}],
            "searches": [{"q": "καταγγελία σύμβασης εργασίας", "kinds": ["code"], "acts": ["ΚΑΕΔ"]}]}
    assert {u["kind"] for u in retrieve(store, plan, Options(kinds=["law"]), [], 10)} == {"law"}
    only = retrieve(store, plan, Options(acts=["ΚΑΕΔ"]), [], 10)
    assert {u["act_id"] for u in only} == {"codes/labour-code"}


async def test_full_run_numbers_sources_and_checks_citations(settings, store):
    llm = ScriptedLLM(
        plan={"in_scope": True, "question": "Ευθύνη από αδικοπραξία και ηθική βλάβη",
              "searches": [{"q": "αδικοπραξία αποζημίωση ηθική βλάβη", "kinds": ["code"], "acts": ["ΑΚ"]}],
              "citations": [{"act": "ΑΚ", "article": "914"}]},
        select={"selected": [2, 1, 99, "x"], "more_searches": []},   # εκτός ορίων και σκουπίδια αγνοούνται
        answer="Υπόχρεος είναι ο υπαίτιος [1]. Οφείλεται και ικανοποίηση [2, παρ. 3]. Βλ. και [7].")
    events, text = await collect(Pipeline(settings, store, llm), "και η ηθική βλάβη;",
                                 history=[{"role": "user", "content": "Ποιος ευθύνεται από αδικοπραξία;"},
                                          {"role": "assistant", "content": "Ο υπαίτιος (ΑΚ 914)."}])
    assert [ev["type"] for ev in events if ev["type"] != "text"] == \
        ["status", "plan", "status", "status", "sources", "status", "check", "done"]
    sources = one(events, "sources")["sources"]
    assert [(s["n"], s["cite"]) for s in sources] == [(1, "ΑΚ 914"), (2, "ΑΚ 932")]
    assert "text" not in sources[0] and sources[0]["act"]["consolidation"] == "enacted"
    check = one(events, "check")
    assert check["cited"] == [1, 2] and check["invalid"] == [7]
    assert check["unverified_pins"] == [{"n": 2, "pin": "παρ. 3"}]
    assert text.startswith("Υπόχρεος")

    plan_call = next(u for stage, u in llm.calls if stage == "plan")
    assert "ΠΡΟΗΓΟΥΜΕΝΗ ΣΥΖΗΤΗΣΗ" in plan_call and plan_call.endswith("ΕΡΩΤΗΣΗ: και η ηθική βλάβη;")
    messages = next(m for stage, m in llm.calls if stage == "write")
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    final = messages[-1]["content"]
    assert '<document n="1" title="ΑΚ 914' in final and "OCR" in final
    assert "όπως αρχικά δημοσιεύθηκε (π.δ. 456/1984" in final
    assert "Κανένα άλλο άρθρο" in final and final.endswith("ΕΡΩΤΗΣΗ: και η ηθική βλάβη;")


async def test_off_topic_and_nothing_found_do_not_call_the_writer(settings, store):
    llm = ScriptedLLM(plan={"in_scope": False})
    events, text = await collect(Pipeline(settings, store, llm), "γεια")
    assert text == OFF_TOPIC and not any(stage == "write" for stage, _ in llm.calls)

    llm = ScriptedLLM(plan={"searches": [{"q": "ξυλοφόρτωση πυρηνικών υποβρυχίων"}]})
    events, text = await collect(Pipeline(settings, store, llm), "ξυλοφόρτωση πυρηνικών υποβρυχίων;")
    assert text == NOTHING_FOUND and one(events, "sources")["sources"] == []
    assert not any(stage == "write" for stage, _ in llm.calls)


async def test_selector_can_reject_everything(settings, store):
    llm = ScriptedLLM(select={"selected": [], "more_searches": []})
    _, text = await collect(Pipeline(settings, store, llm), "καταγγελία σύμβασης εργασίας")
    assert text == NOTHING_FOUND


async def test_planner_and_selector_failures_fall_back_to_plain_search(settings, store):
    llm = ScriptedLLM(plan=LLMError("κάτω"), select=LLMError("κάτω"), answer="Εντάξει [1].")
    events, text = await collect(Pipeline(settings, store, llm), "καταγγελία σύμβασης εργασίας προειδοποίηση")
    assert one(events, "plan")["fallback"] is True
    assert one(events, "sources")["sources"] and text == "Εντάξει [1]."


async def test_writer_failure_becomes_an_error_event(settings, store):
    llm = ScriptedLLM(answer=LLMError("ο πάροχος έπεσε"))
    events, _ = await collect(Pipeline(settings, store, llm), "καταγγελία σύμβασης εργασίας")
    assert events[-1] == {"type": "error", "message": "ο πάροχος έπεσε"}


async def test_selector_may_ask_for_one_more_search(settings, store):
    llm = ScriptedLLM(plan={"searches": [{"q": "καταγγελία σύμβασης εργασίας"}]},
                      select=[{"selected": [1], "more_searches": [{"q": "χρηματική ικανοποίηση ηθική βλάβη"}]},
                              {"selected": [1, 2, 3], "more_searches": [{"q": "δεν τρέχει ξανά"}]}])
    events, _ = await collect(Pipeline(settings, store, llm), "ερώτηση")
    assert sum(1 for stage, _ in llm.calls if stage == "select") == 2
    second = [u for stage, u in llm.calls if stage == "select"][1]
    assert "ΑΚ 932" in second and "προηγούμενη απάντηση" in second   # ό,τι κρατήθηκε μένει υποψήφιο


class FakeLaw:
    enabled = True

    def __init__(self, result):
        self.result, self.calls = result, []

    async def read_as_of(self, paths, date):
        self.calls.append((paths, date))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def test_as_of_date_replaces_text_and_drops_what_was_not_in_force(settings, store):
    base = "greece/national/acts/codes/civil-code/"
    law = FakeLaw({base + "art-914.md": {"in_force": True, "law": "π.δ. 456/1984", "since": "1984-10-24",
                                          "text": "# Άρθρο 914\n\nΈννοια. Παλαιά διατύπωση του άρθρου."},
                   base + "art-932.md": {"in_force": False, "law": "", "since": "", "text": ""}})
    llm = ScriptedLLM(plan={"searches": [], "citations": [{"act": "ΑΚ", "article": "914"}, {"act": "ΑΚ", "article": "932"}]},
                      answer="Ίσχυε η παλαιά διατύπωση [1].")
    events, _ = await collect(Pipeline(settings, store, llm, law), "αδικοπραξία", options=Options(as_of="1990-01-01"))
    assert law.calls == [([base + "art-914.md", base + "art-932.md"], "1990-01-01")]
    first, second = one(events, "sources")["sources"]
    assert first["as_of"] == "1990-01-01" and first["attached"]
    assert second["not_in_force"] == "1990-01-01" and second["attached"] is False
    final = next(m for stage, m in llm.calls if stage == "write")[-1]["content"]
    assert "Παλαιά διατύπωση του άρθρου." in final and "όπως ίσχυε στις 1990-01-01" in final
    assert 'n="2"' not in final and one(events, "check")["cited"] == [1]


async def test_as_of_date_failure_is_a_warning_not_an_error(settings, store):
    law = FakeLaw(LawAPIError("Εξαντλήθηκε το ημερήσιο όριο", limited=True))
    llm = ScriptedLLM(plan={"searches": [], "citations": [{"act": "ΑΚ", "article": "914"}]})
    events, text = await collect(Pipeline(settings, store, llm, law), "αδικοπραξία", options=Options(as_of="1990-01-01"))
    sources = one(events, "sources")
    assert "δεν ελέγχθηκε" in sources["warnings"][0] and sources["sources"][0]["as_of"] is None
    assert text == "Απάντηση [1]."
