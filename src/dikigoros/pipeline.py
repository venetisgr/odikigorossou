"""Η ροή μιας ερώτησης: σχεδιασμός → αναζήτηση → επιλογή άρθρων → σύνταξη → έλεγχος παραπομπών.

Ο συντάκτης βλέπει μόνο τα πλήρη κείμενα των άρθρων που επιλέχθηκαν, και κάθε παραπομπή
της απάντησης ελέγχεται εδώ, στον κώδικα, ότι δείχνει σε ένα από αυτά.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

from .config import Settings
from .corpus import parse_article_file
from .lawapi import LawAPI, LawAPIError
from .llm import LLMError
from .store import KINDS, Store

PROMPTS = Path(__file__).parent / "prompts"
RRF_K = 20
KIND_ORDER = {k: i for i, k in enumerate(KINDS)}
TIERS = ((("constitution", "code"), 8), (("law",), 6), (("decree",), 4))

OFF_TOPIC = ("Η ερώτηση δεν φαίνεται να αφορά ζήτημα δικαίου. Η υπηρεσία απαντά σε ερωτήσεις πάνω στην "
             "ελληνική νομοθεσία (Σύνταγμα, κώδικες, νόμοι, προεδρικά διατάγματα).")
NOTHING_FOUND = ("Δεν εντοπίστηκαν στο ευρετήριο διατάξεις σχετικές με την ερώτηση, οπότε δεν δίνεται απάντηση. "
                 "Το ευρετήριο καλύπτει το Σύνταγμα, είκοσι κώδικες και μέρος μόνο των νόμων και των προεδρικών "
                 "διαταγμάτων· δεν περιέχει νομολογία, υπουργικές αποφάσεις ή εγκυκλίους. Δοκιμάστε να "
                 "διατυπώσετε την ερώτηση με τους όρους του νόμου ή να αφαιρέσετε φίλτρα.")

_BRACKET = re.compile(r"\[([^\[\]\n]{1,80})\]")
_NUMBERS = re.compile(r"\d{1,3}(?:\s*[,;]\s*\d{1,3})*")
_PINNED = re.compile(r"(\d{1,3})\s*[,›>:·]\s*(.+)")
_PARAGRAPH = re.compile(r"παρ(?:άγραφος|\.)?\s*(\d{1,3})", re.I)


@dataclass
class Options:
    kinds: list[str] = field(default_factory=list)   # κενό = όλα τα είδη
    acts: list[str] = field(default_factory=list)    # κενό = όλες οι πράξεις
    as_of: str = ""                                  # YYYY-MM-DD: η διάταξη όπως ίσχυε τότε


def load_prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------- παραπομπές


def extract_citations(text: str) -> list[tuple[int, str]]:
    """Οι παραπομπές μιας απάντησης ως (αριθμός εγγράφου, εντοπισμός): ``[3]``, ``[3, παρ. 2]``, ``[3][5]``."""
    out = []
    for m in _BRACKET.finditer(text):
        inner = m.group(1).strip()
        if _NUMBERS.fullmatch(inner):
            out.extend((int(n), "") for n in re.findall(r"\d+", inner))
            continue
        pinned = _PINNED.fullmatch(inner)
        if pinned:
            out.append((int(pinned.group(1)), pinned.group(2).strip()))
    return out


def check_citations(answer: str, sources: list[dict]) -> dict:
    """Ελέγχει ότι κάθε παραπομπή δείχνει σε συνημμένο έγγραφο και ότι η παράγραφος υπάρχει σε αυτό."""
    by_n = {s["n"]: s for s in sources if s.get("attached", True)}
    cited, invalid, pins = [], [], []
    for n, pin in extract_citations(answer):
        if n not in by_n:
            if n not in invalid:
                invalid.append(n)
            continue
        if n not in cited:
            cited.append(n)
        para = _PARAGRAPH.search(pin)
        if para and not re.search(rf"(?:^|\n|\s){para.group(1)}\s*[.)]\s", by_n[n]["text"]):
            if {"n": n, "pin": pin} not in pins:
                pins.append({"n": n, "pin": pin})
    return {
        "cited": sorted(cited),
        "invalid": sorted(invalid),
        "unverified_pins": pins,
        "uncited": sorted(n for n in by_n if n not in cited),
        "no_citations": bool(by_n) and not cited and len(answer) > 400,
    }


# ---------------------------------------------------------------- αναζήτηση


def _normalise_plan(raw: dict, question: str) -> dict:
    """Ό,τι επέστρεψε ο planner, σε προβλέψιμο σχήμα (τα μοντέλα δεν το τηρούν πάντα)."""
    searches = []
    for item in raw.get("searches") or []:
        if isinstance(item, str):
            item = {"q": item}
        if not isinstance(item, dict) or not str(item.get("q") or "").strip():
            continue
        kinds = [k for k in (item.get("kinds") or []) if k in KINDS] if isinstance(item.get("kinds"), list) else []
        acts = [str(a) for a in item.get("acts") or []] if isinstance(item.get("acts"), list) else []
        searches.append({"q": str(item["q"]).strip()[:300], "kinds": kinds, "acts": acts})
    citations = []
    for item in raw.get("citations") or []:
        if isinstance(item, dict) and item.get("act") and item.get("article") is not None:
            citations.append({"act": str(item["act"])[:80], "article": str(item["article"])[:12]})
    return {
        "in_scope": raw.get("in_scope") is not False,
        "question": str(raw.get("question") or question).strip()[:2000] or question,
        "searches": searches[:8],
        "citations": citations[:12],
    }


def retrieve(store: Store, plan: dict, options: Options, previous: list[str], limit: int) -> list[dict]:
    """Οι υποψήφιες διατάξεις: ένωση των αναζητήσεων (reciprocal rank fusion) και των άμεσων παραπομπών."""
    allowed_kinds = [k for k in options.kinds if k in KINDS]
    allowed_acts = [a for a in (store.resolve_act(x) for x in options.acts) if a]
    scores: dict[str, float] = {}
    units: dict[str, dict] = {}

    def add(hits: list[dict], weight: float = 1.0, bonus: float = 0.0, tag: str = "") -> None:
        for rank, unit in enumerate(hits):
            uid = unit["uid"]
            units.setdefault(uid, unit)
            scores[uid] = scores.get(uid, 0.0) + weight / (RRF_K + rank) + bonus
            if tag:
                units[uid].setdefault("via", set()).add(tag)

    def run(query: str, kinds: list[str], acts: list[str], weight: float) -> None:
        if allowed_kinds:
            kinds = [k for k in (kinds or allowed_kinds) if k in allowed_kinds]
            if not kinds:
                return
        if allowed_acts:
            acts = [a for a in (acts or allowed_acts) if a in allowed_acts]
            if not acts:
                return
        if kinds or acts:
            add(store.search(query, kinds=kinds or None, act_ids=acts or None, limit=10), weight)
            return
        for tier, n in TIERS:  # χωριστά ανά είδος, ώστε οι πολλοί νόμοι να μη σκεπάζουν τους κώδικες
            add(store.search(query, kinds=list(tier), limit=n), weight)

    for search in plan["searches"]:
        acts = [a for a in (store.resolve_act(x) for x in search["acts"]) if a]
        run(search["q"], search["kinds"], acts, 1.0)
        if acts or search["kinds"]:
            # Ο περιορισμός του planner μπορεί να είναι λάθος: η ίδια αναζήτηση τρέχει και ελεύθερη, με μικρότερο βάρος.
            run(search["q"], [], [], 0.4)
    run(plan["question"], [], [], 0.6)

    for cite in plan["citations"]:
        act_id = store.resolve_act(cite["act"])
        if not act_id or (allowed_acts and act_id not in allowed_acts):
            continue
        if allowed_kinds and store.act(act_id)["kind"] not in allowed_kinds:
            continue
        add(store.find_article(act_id, cite["article"])[:3], bonus=1.0, tag="παραπομπή")
    add(store.units(previous[:12]), weight=0.3, tag="προηγούμενη απάντηση")

    ranked = sorted(units.values(), key=lambda u: scores[u["uid"]], reverse=True)[:limit]
    for unit in ranked:
        unit["via"] = sorted(unit.get("via", ()))
        unit.setdefault("snippet", unit["text"][:340])
    return ranked


def _status(unit: dict) -> str:
    act = unit["act"]
    bits = []
    if unit.get("as_of"):
        bits.append(f"όπως ίσχυε στις {unit['as_of']}")
    elif act["consolidation"] == "enacted":
        since = f" ({act['enacting']}" + (f", {act['in_force']}" if act["in_force"] else "") + ")" if act["enacting"] else ""
        bits.append(f"όπως αρχικά δημοσιεύθηκε{since}, χωρίς μεταγενέστερες τροποποιήσεις")
    elif act["consolidation"] == "amended":
        bits.append("με ενσωματωμένες τροποποιήσεις")
    else:
        bits.append("κωδικοποίηση")
    if unit["ocr"]:
        bits.append("OCR")
    return " · ".join(bits)


def _title(unit: dict) -> str:
    act = unit["act"]
    name = act["title"] if act["kind"] in ("code", "constitution") else f"{act['short']} ({act['title'][:140]})"
    head = f": {unit['heading']}" if unit["heading"] else ""
    return f"{unit['cite']} — {name}, {unit['label']}{head}".replace('"', "'")


def candidate_list(candidates: list[dict]) -> str:
    lines = []
    for n, unit in enumerate(candidates, 1):
        flags = " · ".join([_status(unit), *unit["via"]])
        lines.append(f"[{n}] {_title(unit)}\n    ({flags})\n    {unit['snippet']}")
    return "\n".join(lines)


def source_meta(unit: dict) -> dict:
    """Ό,τι στέλνεται στο UI για μια πηγή (χωρίς το κείμενο· αυτό ζητείται όταν ανοίξει)."""
    return {k: unit.get(k) for k in ("n", "uid", "cite", "label", "heading", "context", "chars", "ocr",
                                     "kind", "act", "as_of", "as_of_law", "attached", "not_in_force", "path")}


def documents_block(sources: list[dict]) -> str:
    parts = []
    for s in sources:
        if not s["attached"]:
            continue
        context = f"Θέση: {s['context']}\n" if s["context"] else ""
        parts.append(f'<document n="{s["n"]}" title="{_title(s)}" status="{_status(s)}">\n'
                     f"{context}{s['text']}\n</document>")
    listing = "\n".join(f"[{s['n']}] {s['cite']}" for s in sources if s["attached"])
    return ("\n\n".join(parts) + "\n\nΣυνημμένα έγγραφα:\n" + listing +
            "\n\nΚανένα άλλο άρθρο, νόμος ή απόφαση δεν είναι διαθέσιμα σε αυτή την ερώτηση.")


def history_messages(history: list[dict], turns: int) -> list[dict]:
    """Οι τελευταίες ανταλλαγές ως κανονικά μηνύματα, κομμένες ώστε να μη βαραίνουν το πλαίσιο."""
    out = []
    for msg in history[-2 * turns:]:
        role = msg.get("role")
        content = str(msg.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        cap = 1500 if role == "user" else 4000
        out.append({"role": role, "content": content[:cap] + (" […]" if len(content) > cap else "")})
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def history_digest(history: list[dict], turns: int) -> str:
    lines = []
    for msg in history_messages(history, turns):
        who = "ΧΡΗΣΤΗΣ" if msg["role"] == "user" else "ΒΟΗΘΟΣ"
        lines.append(f"{who}: {msg['content'][:800]}")
    return "\n".join(lines)


# ---------------------------------------------------------------- η ροή


class Pipeline:
    def __init__(self, settings: Settings, store: Store, llm, law: LawAPI | None = None):
        self.settings = settings
        self.store = store
        self.llm = llm
        self.law = law
        self._planner = load_prompt("planner")
        self._selector = load_prompt("selector")
        self._writer = load_prompt("writer")

    def catalogue_text(self) -> str:
        lines = []
        for act in self.store.catalogue():
            state = {"enacted": "όπως αρχικά δημοσιεύθηκε", "amended": "με τροποποιήσεις",
                     "consolidated": "κωδικοποίηση"}[act["consolidation"]]
            basis = f", {act['enacting']}" if act["enacting"] else ""
            lines.append(f"- {act['short']}: {act['title']} ({state}{basis})")
        return "\n".join(lines)

    async def _plan(self, question: str, history: list[dict]) -> dict:
        system = self._planner.format(today=dt.date.today().isoformat(), catalogue=self.catalogue_text())
        digest = history_digest(history, self.settings.history_turns)
        user = (f"ΠΡΟΗΓΟΥΜΕΝΗ ΣΥΖΗΤΗΣΗ:\n{digest}\n\n" if digest else "") + f"ΕΡΩΤΗΣΗ: {question}"
        try:
            raw = await self.llm.complete_json(system, user, stage="plan")
        except LLMError:
            raw = {}
        plan = _normalise_plan(raw, question)
        plan["fallback"] = not raw
        return plan

    async def _select(self, question: str, candidates: list[dict]) -> tuple[list[int], list[dict]]:
        system = self._selector.format(max_sources=self.settings.max_sources)
        user = f"ΕΡΩΤΗΣΗ: {question}\n\nΥΠΟΨΗΦΙΑ ΑΡΘΡΑ:\n{candidate_list(candidates)}"
        try:
            raw = await self.llm.complete_json(system, user, stage="select")
        except LLMError:
            return list(range(1, min(len(candidates), 8) + 1)), []
        picked = []
        for n in raw.get("selected") or []:
            try:
                n = int(n)
            except (TypeError, ValueError):
                continue
            if 1 <= n <= len(candidates) and n not in picked:
                picked.append(n)
        more = _normalise_plan({"searches": raw.get("more_searches") or []}, question)["searches"][:3]
        return picked[: self.settings.max_sources], more

    async def _apply_date(self, sources: list[dict], date: str) -> str:
        """Αντικαθιστά τα κείμενα με όσα ίσχυαν στην ημερομηνία· επιστρέφει προειδοποίηση ή κενό."""
        paths = [s["path"] for s in sources if s["path"].endswith(".md")]
        if not self.law or not self.law.enabled or not paths:
            return ("Η ισχύς σε ημερομηνία δεν ελέγχθηκε: οι πηγές αυτής της απάντησης δεν παρακολουθούνται "
                    "ανά άρθρο στο αρχείο Global law.")
        try:
            dated = await self.law.read_as_of(paths, date)
        except LawAPIError as exc:
            return f"Η ισχύς στις {date} δεν ελέγχθηκε ({exc}). Η απάντηση στηρίζεται στα κείμενα του ευρετηρίου."
        unchecked = 0
        for s in sources:
            info = dated.get(s["path"])
            if not info or s["part"]:
                unchecked += 1
                continue
            if not info["in_force"] or not info["text"]:
                s["attached"] = False
                s["not_in_force"] = date
                continue
            parsed = parse_article_file(info["text"], s["kind"])
            s["text"] = parsed["text"] or s["text"]
            s["as_of"], s["as_of_law"] = date, info["law"]
        if unchecked:
            return f"{unchecked} από τις πηγές δεν ελέγχθηκαν ως προς την ισχύ τους στις {date}."
        return ""

    async def run(self, question: str, history: list[dict] | None = None, options: Options | None = None,
                  previous_sources: list[str] | None = None) -> AsyncIterator[dict]:
        history = history or []
        options = options or Options()
        s = self.settings
        t0 = time.time()
        timings: dict[str, float] = {}

        def lap(name: str, since: float) -> float:
            timings[name] = round(time.time() - since, 2)
            return time.time()

        yield {"type": "status", "stage": "planning", "message": "Ανάλυση της ερώτησης"}
        t = time.time()
        plan = await self._plan(question, history)
        t = lap("plan", t)
        yield {"type": "plan", "question": plan["question"], "searches": plan["searches"],
               "citations": plan["citations"], "fallback": plan["fallback"]}
        if not plan["in_scope"]:
            yield {"type": "text", "delta": OFF_TOPIC}
            yield {"type": "done", "sources": 0, "timings": timings, "seconds": round(time.time() - t0, 2)}
            return

        yield {"type": "status", "stage": "searching", "message": "Αναζήτηση διατάξεων"}
        candidates = await asyncio.to_thread(retrieve, self.store, plan, options, previous_sources or [],
                                             s.max_candidates)
        t = lap("search", t)
        picked: list[int] = []
        if candidates:
            yield {"type": "status", "stage": "selecting",
                   "message": f"Επιλογή ανάμεσα σε {len(candidates)} υποψήφιες διατάξεις"}
            picked, more = await self._select(plan["question"], candidates)
            if more:
                # Μία, το πολύ, συμπληρωματική αναζήτηση για διάταξη που έλειπε.
                keep = [candidates[n - 1]["uid"] for n in picked]
                extra_plan = {"question": plan["question"], "searches": more, "citations": plan["citations"]}
                candidates = await asyncio.to_thread(retrieve, self.store, extra_plan, options,
                                                     keep + (previous_sources or []), s.max_candidates)
                picked, _ = await self._select(plan["question"], candidates)
            t = lap("select", t)

        sources = [dict(candidates[n - 1]) for n in picked]
        sources.sort(key=lambda u: (KIND_ORDER[u["kind"]], u["act_id"], u["num"], u["suffix"], u["part"]))
        budget = s.context_chars
        for n, src in enumerate(sources, 1):
            src["n"] = n
            src["attached"] = len(src["text"]) <= budget
            budget -= len(src["text"]) if src["attached"] else 0

        warnings = []
        if sources and options.as_of:
            yield {"type": "status", "stage": "reading", "message": f"Ανάγνωση των διατάξεων όπως ίσχυαν στις {options.as_of}"}
            note = await self._apply_date(sources, options.as_of)
            if note:
                warnings.append(note)
            t = lap("as_of", t)
        yield {"type": "sources", "sources": [source_meta(x) for x in sources], "warnings": warnings}

        attached = [x for x in sources if x["attached"]]
        if not attached:
            yield {"type": "text", "delta": NOTHING_FOUND}
            yield {"type": "done", "sources": 0, "timings": timings, "seconds": round(time.time() - t0, 2)}
            return

        yield {"type": "status", "stage": "writing", "message": "Σύνταξη της απάντησης"}
        messages = [{"role": "system", "content": self._writer}]
        messages += history_messages(history, s.history_turns)
        messages.append({"role": "user", "content": documents_block(sources) + f"\n\nΕΡΩΤΗΣΗ: {question}"})
        answer = []
        try:
            async for piece in self.llm.stream(messages):
                answer.append(piece)
                yield {"type": "text", "delta": piece}
        except LLMError as exc:
            yield {"type": "error", "message": str(exc)}
            return
        lap("write", t)
        yield {"type": "check", **check_citations("".join(answer), sources)}
        yield {"type": "done", "sources": len(attached), "timings": timings, "seconds": round(time.time() - t0, 2)}
