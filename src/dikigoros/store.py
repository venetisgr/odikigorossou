"""Ανάγνωση του ευρετηρίου: αναζήτηση BM25, εύρεση άρθρου από παραπομπή, κείμενα μονάδων."""

from __future__ import annotations

import re
import sqlite3
import threading
import zlib
from pathlib import Path

from .greek import fold, stems, tokens, stem

KINDS = ("constitution", "code", "law", "decree")
KIND_LABEL = {"constitution": "Σύνταγμα", "code": "Κώδικες", "law": "Νόμοι", "decree": "Προεδρικά διατάγματα"}
HEAD_WEIGHT = 6.0
INTRO_LAW_OFFSET = 50_000

# Ονομασίες με τις οποίες αναφέρονται συνήθως οι κώδικες, πέρα από τη συντομογραφία τους.
_CODE_ALIASES = {
    "civil-code": ["αστικος κωδικας", "αστικου κωδικα", "α.κ."],
    "civil-procedure-code": ["κωδικας πολιτικης δικονομιας", "κωδικα πολιτικης δικονομιας", "κ.πολ.δ.", "κπολδ"],
    "penal-code": ["ποινικος κωδικας", "ποινικου κωδικα", "π.κ."],
    "criminal-procedure-code": ["κωδικας ποινικης δικονομιας", "κωδικα ποινικης δικονομιας", "κ.π.δ.", "κποινδ"],
    "administrative-procedure-code": ["κωδικας διοικητικης διαδικασιας", "κωδικα διοικητικης διαδικασιας", "κδδιαδ"],
    "code-of-administrative-procedure-courts": ["κωδικας διοικητικης δικονομιας", "κωδικα διοικητικης δικονομιας", "κ.δ.δ.", "κδδικ"],
    "labour-code": ["κωδικας ατομικου εργατικου δικαιου", "κωδικα ατομικου εργατικου δικαιου", "εργατικος κωδικας"],
    "lawyers-code": ["κωδικας δικηγορων", "κωδικα δικηγορων", "κωδ.δικ."],
    "civil-servants-code": ["υπαλληλικος κωδικας", "υπαλληλικου κωδικα", "υ.κ."],
    "courts-organisation-code": ["κωδικας οργανισμου δικαστηριων", "κοδκδλ"],
    "road-traffic-code": ["κωδικας οδικης κυκλοφοριας", "κωδικα οδικης κυκλοφοριας", "κ.ο.κ."],
    "income-tax-code": ["κωδικας φορολογιας εισοδηματος", "κ.φ.ε."],
    "vat-code": ["κωδικας φπα", "κωδικας φορου προστιθεμενης αξιας"],
    "tax-procedure-code": ["κωδικας φορολογικης διαδικασιας", "κ.φ.δ."],
    "public-revenue-collection-code": ["κωδικας εισπραξης δημοσιων εσοδων", "κ.ε.δ.ε."],
    "municipalities-code": ["κωδικας δημων και κοινοτητων", "κ.δ.κ."],
    "customs-code": ["τελωνειακος κωδικας", "εθνικος τελωνειακος κωδικας"],
}
_STATUTE_REF = re.compile(
    r"(?P<kind>π\s*\.?\s*δ\s*\.?|προεδρικο\s+διαταγμα|ν\s*\.|νομοσ|νομου|n\s*\.)?\s*"
    r"(?:υπ\W{0,3}αριθμ?\W{0,2})?(?P<num>\d{1,4})\s*/\s*(?P<year>(?:19|20)\d{2})"
)
_SENTENCE = re.compile(r"(?<=[.;·:])\s+|\n+")


def _key(text: str) -> str:
    return re.sub(r"[\s.\-’'`]+", "", fold(text))


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(
                f"Δεν βρέθηκε το ευρετήριο στο {self.path}. Χτίστε το: python -m dikigoros.indexer --source …"
            )
        self._db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._acts: dict[str, dict] = {r["id"]: dict(r) for r in self._q("SELECT * FROM acts")}
        self._aliases: dict[str, str] = {}
        for act in self._acts.values():
            if act["kind"] in ("code", "constitution"):
                slug = act["id"].split("/")[-1]
                for name in (act["short"], act["title"], slug, act["id"], *_CODE_ALIASES.get(slug, [])):
                    self._aliases[_key(name)] = act["id"]
        for name in ("συνταγμα", "συνταγματος", "σ", "συντ"):
            self._aliases[_key(name)] = "constitution"
        # «ν. 4194/2013» → Κώδικας Δικηγόρων: ο νόμος που κύρωσε έναν κώδικα οδηγεί στον κώδικα.
        self._enacting: dict[tuple[str, str], str] = {}
        for act in self._acts.values():
            m = _STATUTE_REF.search(fold(act["enacting"] or "")) if act["kind"] == "code" else None
            if m:
                self._enacting[(m.group("num"), m.group("year"))] = act["id"]

    def _q(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    # ------------------------------------------------------------ πράξεις

    def meta(self) -> dict:
        out = {r["key"]: r["value"] for r in self._q("SELECT key, value FROM meta")}
        counts = {k: 0 for k in KINDS}
        for act in self._acts.values():
            counts[act["kind"]] += 1
        out["by_kind"] = counts
        return out

    def act(self, act_id: str) -> dict | None:
        return self._acts.get(act_id)

    def catalogue(self) -> list[dict]:
        """Σύνταγμα και κώδικες: ο κατάλογος που βλέπουν ο planner και το UI."""
        acts = [a for a in self._acts.values() if a["kind"] in ("constitution", "code")]
        acts.sort(key=lambda a: (a["kind"] != "constitution", a["title"]))
        return acts

    def resolve_act(self, ref: str) -> str | None:
        """Αναγνωριστικό πράξης από ό,τι θα έγραφε ένας νομικός: «ΑΚ», «ν. 4624/2019», «π.δ. 80/2022»."""
        if not ref:
            return None
        ref = ref.strip()
        if ref in self._acts:
            return ref
        hit = self._aliases.get(_key(ref))
        if hit:
            return hit
        m = _STATUTE_REF.search(fold(ref))
        if m:
            num, year = m.group("num"), m.group("year")
            kind = (m.group("kind") or "").replace(" ", "")
            order = ["decrees", "laws"] if kind.startswith("π") else ["laws", "decrees"]
            for base in order:
                for name in (f"{num}-me-tropopoiiseis", num):
                    if f"{base}/{year}/{name}" in self._acts:
                        return f"{base}/{year}/{name}"
            return self._enacting.get((num, year))
        return None

    def search_acts(self, query: str, kinds: list[str] | None = None, limit: int = 10) -> list[dict]:
        """Πράξεις με βάση τον τίτλο τους."""
        match = _match(stems(query))
        if not match:
            return []
        sql = ("SELECT a.* FROM acts_fts JOIN acts a ON a.rowid = acts_fts.rowid "
               "WHERE acts_fts MATCH ?")
        params: list = [match]
        if kinds:
            sql += f" AND a.kind IN ({','.join('?' * len(kinds))})"
            params += kinds
        sql += " ORDER BY bm25(acts_fts) LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._q(sql, params)]

    def toc(self, act_id: str, limit: int = 3000) -> list[dict]:
        rows = self._q("SELECT uid, label, heading, context, chars FROM units WHERE act_id = ? "
                       "ORDER BY num, suffix, part LIMIT ?", (act_id, limit))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ μονάδες

    def _unit(self, row: sqlite3.Row, with_text: bool = True) -> dict:
        act = self._acts[row["act_id"]]
        unit = {k: row[k] for k in ("id", "uid", "act_id", "kind", "label", "num", "suffix", "part",
                                    "heading", "context", "chars", "path")}
        unit["ocr"] = bool(row["ocr"])
        unit["cite"] = cite_label(act, unit)
        unit["act"] = {k: act[k] for k in ("id", "kind", "title", "short", "consolidation", "note",
                                           "official_url", "in_force", "enacting")}
        unit["act"]["ocr"] = bool(act["ocr"])
        if with_text:
            unit["text"] = zlib.decompress(row["body"]).decode("utf-8")
        return unit

    def unit(self, uid: str) -> dict | None:
        rows = self._q("SELECT * FROM units WHERE uid = ?", (uid,))
        return self._unit(rows[0]) if rows else None

    def units(self, uids: list[str]) -> list[dict]:
        if not uids:
            return []
        rows = self._q(f"SELECT * FROM units WHERE uid IN ({','.join('?' * len(uids))})", uids)
        by_uid = {r["uid"]: self._unit(r) for r in rows}
        return [by_uid[u] for u in uids if u in by_uid]

    def find_article(self, act_id: str, number: str | int, intro_law: bool = False) -> list[dict]:
        """Όλα τα τμήματα ενός άρθρου· ο αριθμός μπορεί να έχει γράμμα («5Α»)."""
        m = re.match(r"\s*(\d+)\s*([^\W\d_]{0,2})", fold(str(number)))
        if not m:
            return []
        num = int(m.group(1)) + (INTRO_LAW_OFFSET if intro_law else 0)
        suffix = m.group(2).upper().translate(str.maketrans("ABEZHIKMNOPTXY", "ΑΒΕΖΗΙΚΜΝΟΡΤΧΥ"))
        rows = self._q("SELECT * FROM units WHERE act_id = ? AND num = ? AND suffix = ? ORDER BY part",
                       (act_id, num, suffix))
        return [self._unit(r) for r in rows]

    def neighbours(self, unit: dict) -> dict:
        """Το προηγούμενο και το επόμενο άρθρο της ίδιας πράξης, για την πλοήγηση στο UI."""
        key = (unit["act_id"], unit["num"], unit["suffix"], unit["part"])
        prev = self._q("SELECT uid, label FROM units WHERE act_id = ? AND (num, suffix, part) < (?, ?, ?) "
                       "ORDER BY num DESC, suffix DESC, part DESC LIMIT 1", key)
        nxt = self._q("SELECT uid, label FROM units WHERE act_id = ? AND (num, suffix, part) > (?, ?, ?) "
                      "ORDER BY num, suffix, part LIMIT 1", key)
        return {"prev": dict(prev[0]) if prev else None, "next": dict(nxt[0]) if nxt else None}

    # ------------------------------------------------------------ αναζήτηση

    def search(self, query: str, kinds: list[str] | None = None, act_ids: list[str] | None = None,
               limit: int = 20, pool: int = 60) -> list[dict]:
        """Οι μονάδες που ταιριάζουν καλύτερα στο ερώτημα, με απόσπασμα.

        Το FTS δίνει την πρώτη κατάταξη (BM25, ο τίτλος μετρά περισσότερο)· μετά ευνοούνται
        όσες μονάδες περιέχουν περισσότερους από τους όρους του ερωτήματος.
        """
        q_stems = list(dict.fromkeys(s for s in stems(query) if not (s.isdigit() and len(s) < 3)))[:24]
        match = _match(q_stems)
        if not match:
            return []
        sql = ("SELECT u.*, bm25(units_fts, ?, 1.0) AS score FROM units_fts "
               "JOIN units u ON u.id = units_fts.rowid WHERE units_fts MATCH ?")
        params: list = [HEAD_WEIGHT, match]
        if kinds:
            sql += f" AND u.kind IN ({','.join('?' * len(kinds))})"
            params += kinds
        if act_ids:
            sql += f" AND u.act_id IN ({','.join('?' * len(act_ids))})"
            params += act_ids
        sql += " ORDER BY score LIMIT ?"
        params.append(max(limit, pool))
        hits = []
        wanted = set(q_stems)
        for row in self._q(sql, params):
            unit = self._unit(row)
            head_stems = set(stems(f"{unit['heading']} {unit['context']} {unit['act']['title']}"))
            snippet, body_found = best_snippet(unit["text"], wanted)
            found = body_found | (wanted & head_stems)
            coverage = len(found) / len(wanted)
            unit["score"] = round(-row["score"] * (0.5 + coverage), 4)
            unit["coverage"] = round(coverage, 2)
            unit["snippet"] = snippet
            hits.append(unit)
        hits.sort(key=lambda u: u["score"], reverse=True)
        return hits[:limit]


def _match(q_stems: list[str]) -> str:
    return " OR ".join(f'"{s}"' for s in q_stems if s)


def cite_label(act: dict, unit: dict) -> str:
    """Σύντομη παραπομπή όπως θα τη γράφαμε σε δικόγραφο: «ΑΚ 914», «άρθρο 19 ν. 5053/2023»."""
    num = unit["num"]
    intro = num >= INTRO_LAW_OFFSET and num < 100_000
    number = f"{num - INTRO_LAW_OFFSET if intro else num}{unit['suffix']}"
    part = f" (τμ. {unit['part']})" if unit["part"] else ""
    if act["kind"] in ("code", "constitution"):
        if not unit["label"].startswith("Άρθρο"):
            return f"{unit['label'].split(' (τμήμα')[0]} {act['short']}{part}"
        short = f"ΕισΝ{act['short']}" if intro else act["short"]
        return f"{short} {number}{part}"
    if unit["label"].startswith("Άρθρο"):
        return f"άρθρο {number} {act['short']}{part}"
    if unit["label"].startswith("Πλήρες"):
        return f"{act['short']}{part}"
    return f"{unit['label']} {act['short']}"


def best_snippet(text: str, wanted: set[str], width: int = 340) -> tuple[str, set[str]]:
    """Το σημείο του κειμένου με τους περισσότερους όρους του ερωτήματος, και ποιοι όροι βρέθηκαν."""
    segments = [s.strip() for s in _SENTENCE.split(text) if s.strip()]
    found_all: set[str] = set()
    best, best_score = 0, -1
    seg_hits = []
    for i, seg in enumerate(segments):
        hit = {stem(t) for t in tokens(seg)} & wanted
        seg_hits.append(hit)
        found_all |= hit
        score = len(hit) * 10 - min(len(seg), 400) / 400
        if score > best_score:
            best, best_score = i, score
    if not segments:
        return "", found_all
    out = segments[best]
    j = best + 1
    while len(out) < width and j < len(segments):
        out += " " + segments[j]
        j += 1
    if len(out) > width:
        out = out[:width].rsplit(" ", 1)[0] + "…"
    return ("… " if best > 0 else "") + out, found_all
