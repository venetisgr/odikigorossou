"""Ανάγνωση ενός checkout του lexifina/greece-law σε «πράξεις» και «μονάδες» (άρθρα).

Το αποθετήριο κρατά τα κείμενα σε τρεις μορφές, και οι τρεις διαβάζονται εδώ:

* ``art-NNN.md``: ένα αρχείο ανά άρθρο (Σύνταγμα, κώδικες, ~600 νόμοι/π.δ.).
* ``text-layer.txt``: ολόκληρο το ΦΕΚ ως text layer του PDF (οι περισσότεροι νόμοι/π.δ.)·
  το χωρίζουμε εμείς σε άρθρα.
* ``text.md``: OCR σαρωμένου ΦΕΚ (παλαιοί νόμοι)· επίσης το χωρίζουμε σε άρθρα.

Μονάδα ανάκτησης είναι πάντα το άρθρο· πολύ μεγάλα άρθρα σπάνε σε τμήματα, αυτούσια.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .greek import article_heading, fold

API_PREFIX = "greece/national/acts/"
MAX_UNIT_CHARS = 12_000
MAX_PARTS = 8
MIN_LETTER_RATIO = 0.45

# Συντομογραφίες όπως τις γράφουν οι νομικοί.
CODE_ABBR = {
    "civil-code": "ΑΚ",
    "civil-procedure-code": "ΚΠολΔ",
    "penal-code": "ΠΚ",
    "criminal-procedure-code": "ΚΠΔ",
    "administrative-procedure-code": "ΚΔΔιαδ",
    "code-of-administrative-procedure-courts": "ΚΔΔ",
    "labour-code": "ΚΑΕΔ",
    "lawyers-code": "ΚωδΔικ",
    "civil-servants-code": "ΥΚ",
    "courts-organisation-code": "ΚΟΔΚΔΛ",
    "customs-code": "ΕΤΚ",
    "income-tax-code": "ΚΦΕ",
    "indirect-transaction-taxes-code": "ΚΕΦΣ",
    "municipal-employees-code": "ΚΚΔΚΥ",
    "municipalities-code": "ΚΔΚ",
    "property-tax-code": "ΚΦΠ",
    "public-revenue-collection-code": "ΚΕΔΕ",
    "road-traffic-code": "ΚΟΚ",
    "tax-procedure-code": "ΚΦΔ",
    "vat-code": "ΚΦΠΑ",
}


@dataclass
class Act:
    id: str
    kind: str  # constitution | code | law | decree
    title: str
    short: str
    year: int | None = None
    number: str = ""
    consolidation: str = "enacted"  # enacted | amended | consolidated
    ocr: bool = False
    note: str = ""
    official_url: str = ""
    in_force: str = ""
    enacting: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Unit:
    uid: str
    act_id: str
    label: str
    num: int
    suffix: str = ""
    part: int = 0
    heading: str = ""
    context: str = ""
    text: str = ""
    path: str = ""
    ocr: bool = False
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------- καθάρισμα κειμένου

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_ITEM_START = re.compile(r"^(\d{1,3}[.)]|[α-ωΑ-Ω]{1,3}[.)]|[ivxIVX]{1,4}[.)]|«|-\s|•)")
_STRUCT = re.compile(r"^(άρθρο|αρθρο|κεφαλαιο|μεροσ|τμημα|τιτλοσ|βιβλιο|παραρτημα)\b")
_TERMINAL = (".", ";", ":", "»", "·", "!", "”")
_DIGITS_ONLY = re.compile(r"^[\d\s.\-–]+$")
_LETTERS = re.compile(r"[A-Za-zΑ-Ωα-ωΆ-ώ]")


def _is_furniture(line: str) -> bool:
    """Κεφαλίδες/υποσέλιδα σελίδας του ΦΕΚ που δεν ανήκουν στο κείμενο του νόμου."""
    if _DIGITS_ONLY.match(line):
        return True
    low = fold(line)
    if "εφημερι" in low and "κυβερνησ" in low:
        return True
    return low.startswith("τευχοσ α") and len(low) < 40


def reflow(lines: list[str], furniture: bool = True) -> str:
    """Ενώνει τις σπασμένες από τη σελιδοποίηση γραμμές σε παραγράφους."""
    out: list[str] = []
    cur = ""
    for raw in lines:
        line = raw.replace("\f", "").replace("­", "").strip()
        if not line:
            if cur:
                out.append(cur)
                cur = ""
            continue
        if furniture and _is_furniture(line):
            continue
        if not cur:
            cur = line
            continue
        low = fold(line)
        starts_new = bool(_ITEM_START.match(line)) or bool(_STRUCT.match(low))
        if cur.endswith(("-", "‐", "‑")) and line[:1].islower() and not starts_new:
            cur = cur[:-1] + line  # συλλαβισμός στο τέλος της γραμμής
        elif cur.endswith(_TERMINAL) or starts_new or (cur.isupper() and len(cur) < 90):
            out.append(cur)
            cur = line
        else:
            cur = cur + " " + line
    if cur:
        out.append(cur)
    return "\n".join(re.sub(r"[ \t]{2,}", " ", p) for p in out)


def _letter_ratio(text: str) -> float:
    return len(_LETTERS.findall(text)) / max(1, len(text))


def split_parts(text: str, limit: int = MAX_UNIT_CHARS) -> list[str]:
    """Σπάει ένα μεγάλο άρθρο σε τμήματα στα όρια παραγράφων, αυτούσια."""
    if len(text) <= limit:
        return [text]
    parts, cur, size = [], [], 0
    for para in text.split("\n"):
        while len(para) > limit:  # παράγραφος μεγαλύτερη από το όριο (π.χ. πίνακας)
            cut = para.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            if cur:
                parts.append("\n".join(cur))
                cur, size = [], 0
            parts.append(para[:cut])
            para = para[cut:].lstrip()
        if size + len(para) + 1 > limit and cur:
            parts.append("\n".join(cur))
            cur, size = [], 0
        cur.append(para)
        size += len(para) + 1
    if cur:
        parts.append("\n".join(cur))
    return parts


# ---------------------------------------------------------------- αρχεία ανά άρθρο

_FILE_ART = re.compile(r"^(intro-law-)?art-(\d+)(?:-([a-z]+))?$")
_FILE_ANNEX = re.compile(r"^parartima(?:-(\d+))?$")
_INLINE_NUMBERED = re.compile(r"^([^\d«(][^;:]{2,150}?)\s+(1\.\s+\S.*)$", re.S)
_INLINE_SENTENCE = re.compile(r"^([Α-ΩΆ-Ώ][^.;:\d]{2,70})\.\s+([Α-ΩΆ-Ώ«(0-9᾿'].*)$", re.S)
_LATIN_TO_GREEK_UPPER = str.maketrans(
    {"a": "Α", "b": "Β", "g": "Γ", "d": "Δ", "e": "Ε", "z": "Ζ", "h": "Η", "i": "Ι", "k": "Κ"}
)


def label_from_filename(stem: str) -> tuple[str, int, str]:
    """(ετικέτα, αριθμός, γράμμα) από όνομα αρχείου όπως ``art-005-a``."""
    m = _FILE_ART.match(stem)
    if m:
        num = int(m.group(2))
        suffix = (m.group(3) or "").translate(_LATIN_TO_GREEK_UPPER)
        label = f"Άρθρο {num}{suffix}"
        if m.group(1):
            label += " ΕισΝ"
        return label, num, suffix
    m = _FILE_ANNEX.match(stem)
    if m:
        n = int(m.group(1) or 0)
        return (f"Παράρτημα {n}" if n else "Παράρτημα"), 100_000 + n, ""
    if stem == "prooimio":
        return "Προοίμιο", 0, ""
    return stem, 200_000, ""


def parse_article_file(text: str, kind: str) -> dict:
    """Χωρίζει ένα ``art-NNN.md`` σε πλαίσιο (ΜΕΡΟΣ/ΚΕΦΑΛΑΙΟ), τίτλο άρθρου και σώμα."""
    ocr = "OCR" in "".join(_COMMENT.findall(text))
    text = _COMMENT.sub("", text)
    lines = [ln.rstrip() for ln in text.split("\n")]
    context: list[str] = []
    heading = ""
    body: list[str]

    if lines and lines[0].startswith("# "):
        # Μορφή κωδίκων: «# Άρθρο N», προαιρετικά _πλαίσιο_, τίτλος, σώμα.
        rest = [ln for ln in lines[1:]]
        body = []
        for ln in rest:
            s = ln.strip()
            if not body and s.startswith("_") and s.endswith("_") and len(s) > 2:
                if not s.startswith("_("):  # οι «_(… OCR …)_» είναι σημειώσεις, όχι πλαίσιο
                    context.append(s.strip("_").strip())
                continue
            if not body and not s:
                continue
            body.append(ln)
    else:
        # Μορφή νόμων: δομικές γραμμές, «Άρθρο N», τίτλος, σώμα.
        idx = next((i for i, ln in enumerate(lines[:12]) if article_heading(ln)), None)
        if idx is None:
            body = lines
        else:
            context = [ln.strip() for ln in lines[:idx] if ln.strip()]
            body = lines[idx + 1:]

    while body and not body[0].strip():
        body = body[1:]
    paras = [p for p in body]
    first = paras[0].strip() if paras else ""
    has_more = any(p.strip() for p in paras[1:])
    if first and has_more and len(first) <= 200 and not first.endswith((".", ";")) \
            and not _ITEM_START.match(first):
        heading, paras = first, paras[1:]
    elif first and kind in ("code", "constitution"):
        m = _INLINE_NUMBERED.match(first)
        if m and len(m.group(1).split()) <= 16 and "." not in m.group(1)[:-1]:
            heading, paras = m.group(1).strip(), [m.group(2)] + paras[1:]
        else:
            m = _INLINE_SENTENCE.match(first)
            if m and len(m.group(1).split()) <= 7:
                heading, paras = m.group(1).strip(), [m.group(2)] + paras[1:]

    # Οι κενές γραμμές χωρίζουν παραγράφους, αλλά τα αρχεία σπάνε συχνά και μέσα σε πρόταση
    # («…από δύο / (2) έτη»): ό,τι δεν τελειώνει σε στίξη ενώνεται με τη συνέχειά του.
    body_text = reflow([p for p in paras if p.strip()], furniture=False)
    return {
        "heading": heading.strip(" .·-–"),
        "context": " · ".join(context)[:400],
        "text": body_text,
        "ocr": ocr,
    }


# ---------------------------------------------------------------- ολόκληρα ΦΕΚ → άρθρα


def split_articles(raw: str) -> list[dict]:
    """Χωρίζει το κείμενο ενός ΦΕΚ σε άρθρα, με βάση τις γραμμές «Άρθρο N».

    Δεκτή γίνεται μόνο επικεφαλίδα που συνεχίζει την αρίθμηση (επόμενο ή έως +3), ώστε
    τα άρθρα άλλων νόμων που παρατίθενται μέσα σε τροποποιητικές διατάξεις να μένουν στο σώμα.
    """
    lines = raw.replace("\r", "").split("\n")
    found: list[tuple[int, int, str]] = []
    for i, ln in enumerate(lines):
        if len(ln) > 40 or "ρθρ" not in fold(ln[:12]):
            continue
        hit = article_heading(ln)
        if hit:
            found.append((i, hit[0], hit[1]))
    marks: list[tuple[int, int, str]] = []
    last = 0
    for k, (i, num, suffix) in enumerate(found):
        if not marks:
            ok = num <= 2
        elif num == last + 1 or (num == last and suffix):
            ok = True
        elif last + 1 < num <= last + 3:
            # Άλμα στην αρίθμηση: δεκτό μόνο αν το άρθρο που «λείπει» δεν εμφανίζεται πιο κάτω
            # (τότε η επικεφαλίδα του απλώς δεν αναγνωρίστηκε· αλλιώς αυτή εδώ είναι παράθεμα).
            ok = not any(n == last + 1 for _, n, _ in found[k + 1:])
        else:
            ok = False
        if ok:
            marks.append((i, num, suffix))
            last = num
    if len(marks) < 2:
        return []
    out = []
    for k, (i, num, suffix) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else len(lines)
        chunk = [ln for ln in lines[i + 1:end]]
        heading_lines: list[str] = []
        j = 0
        while j < len(chunk) and not chunk[j].strip():
            j += 1
        while j < len(chunk) and len(heading_lines) < 4:
            s = chunk[j].strip()
            if not s or _ITEM_START.match(s) or len(s) > 130 or _is_furniture(s):
                break
            if heading_lines:
                # Ο τίτλος συνεχίζεται μόνο αν η προηγούμενη γραμμή έμεινε ανοιχτή ή αυτή αρχίζει με πεζό.
                prev = heading_lines[-1]
                if not (prev.endswith(("-", "‐", "–", ",")) or s[:1].islower()):
                    break
            heading_lines.append(s)
            j += 1
        heading = reflow(heading_lines).replace("\n", " ") if heading_lines else ""
        if heading.endswith(".") and len(heading) > 90:  # ήταν πρόταση του σώματος, όχι τίτλος
            heading, j = "", 0
        body = reflow(chunk[j:] if heading else chunk)
        out.append({"num": num, "suffix": suffix, "heading": heading.strip(" ."), "text": body})
    return out


_TITLE_START = re.compile(r"(νομοσ|διαταγμα|ν\s*ο\s*μ\s*ο\s*σ).{0,12}αριθ")
_TITLE_END = re.compile(r"^(ο προεδροσ|εκδιδομε|εχοντασ υποψη|αρθρο|ψηφισαμενοι|κεφαλαιο|μεροσ)")


def title_from_head(raw: str) -> str:
    """Ο τίτλος της πράξης: οι γραμμές ανάμεσα στο «ΝΟΜΟΣ ΥΠ’ ΑΡΙΘ. N» και το «Ο ΠΡΟΕΔΡΟΣ…»."""
    lines = raw.replace("\f", "\n").split("\n")[:60]
    start = None
    for i, ln in enumerate(lines[:25]):
        low = fold(ln).translate(str.maketrans("nom", "νομ"))
        if _TITLE_START.search(low):
            start = i + 1
            break
    if start is None:
        return ""
    picked = []
    for ln in lines[start:start + 14]:
        low = fold(ln).strip()
        if _TITLE_END.match(low):
            break
        picked.append(ln)
    title = reflow(picked).replace("\n", " ").strip(" .")
    return title[:400]


# ---------------------------------------------------------------- πράξεις


def _note(consolidation: str, ocr: bool, enacting: str, in_force: str, detail: str = "") -> str:
    bits = []
    if consolidation == "enacted":
        src = f" ({enacting}" + (f", σε ισχύ από {in_force}" if in_force else "") + ")" if enacting else ""
        bits.append(
            f"Κείμενο όπως αρχικά δημοσιεύθηκε στο ΦΕΚ{src}· δεν ενσωματώνει μεταγενέστερες τροποποιήσεις."
        )
    elif consolidation == "amended":
        bits.append("Κείμενο με ενσωματωμένες τις τροποποιήσεις που παρακολουθεί το αρχείο.")
    elif consolidation == "consolidated":
        bits.append("Διοικητική κωδικοποίηση (ενοποιημένο κείμενο)" + (f": {detail}" if detail else "") + ".")
    if ocr:
        bits.append("Κείμενο από OCR σαρωμένου ΦΕΚ· πιθανά λάθη αναγνώρισης.")
    return " ".join(bits)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _units_from_article_files(root: Path, rel: str, act: Act) -> list[Unit]:
    units = []
    for f in sorted((root / rel).glob("*.md")):
        if f.name == "README.md" or f.name == "text.md":
            continue
        label, num, suffix = label_from_filename(f.stem)
        parsed = parse_article_file(_read(f), act.kind)
        if not parsed["text"] and not parsed["heading"]:
            continue
        if f.stem.startswith("intro-law-"):
            num += 50_000  # ο Εισαγωγικός Νόμος ταξινομείται μετά τον Κώδικα
        units.extend(_emit(act, f"{rel}/{f.stem}", label, num, suffix, parsed["heading"],
                           parsed["context"], parsed["text"], f"{rel}/{f.name}",
                           parsed["ocr"] or act.ocr))
    return units


def _emit(act: Act, uid: str, label: str, num: int, suffix: str, heading: str, context: str,
          text: str, path: str, ocr: bool) -> list[Unit]:
    parts = split_parts(text)[:MAX_PARTS]
    kept = [(i, p) for i, p in enumerate(parts, 1) if len(parts) == 1 or _letter_ratio(p) >= MIN_LETTER_RATIO]
    out = []
    for i, part in kept:
        many = len(parts) > 1
        out.append(Unit(
            uid=f"{uid}~{i}" if many else uid,
            act_id=act.id,
            label=f"{label} (τμήμα {i}/{len(parts)})" if many else label,
            num=num, suffix=suffix, part=i if many else 0,
            heading=heading, context=context, text=part,
            path=API_PREFIX + path, ocr=ocr,
        ))
    return out


def _units_from_whole_text(rel: str, filename: str, raw: str, act: Act) -> list[Unit]:
    articles = split_articles(raw)
    units: list[Unit] = []
    if not articles:
        text = reflow(raw.split("\n"))
        return _emit(act, f"{rel}/full", "Πλήρες κείμενο", 1, "", "", "", text, f"{rel}/{filename}", act.ocr)
    for art in articles:
        label = f"Άρθρο {art['num']}{art['suffix']}"
        uid = f"{rel}/art-{art['num']}{art['suffix'].lower()}"
        units.extend(_emit(act, uid, label, art["num"], art["suffix"], art["heading"], "",
                           art["text"], f"{rel}/{filename}", act.ocr))
    return units


_README_META = re.compile(r"((?:ν|π\.δ)\.\s*\d+/\d{4})")
_README_URL = re.compile(r"<(https?://[^>]+)>")
_README_FORCE = re.compile(r"In force:\s*(\d{4}-\d{2}-\d{2})|latest in force:\s*(\d{4}-\d{2}-\d{2})")
_HISTORY = re.compile(r"History:\s*\*\*([^*]+)\*\*")


def parse_code(root: Path, slug: str) -> tuple[Act, list[Unit]]:
    rel = f"codes/{slug}"
    readme = _read(root / rel / "README.md")
    lines = readme.split("\n")
    title = lines[0].lstrip("# ").split(" · ")[0].strip()
    meta = next((ln for ln in lines[1:6] if ln.strip()), "")
    enacting = (_README_META.search(meta) or _README_META.search(readme[:1500]))
    force = _README_FORCE.search(readme[:2500])
    hist = (_HISTORY.search(readme) or [None, ""])[1].strip().lower()
    if hist.startswith("consolidated"):
        consolidation = "consolidated"
    elif "amended" in hist:
        consolidation = "amended"
    else:
        consolidation = "enacted"
    detail = ""
    m = re.search(r"updated up to ([^·\n]+)", meta)
    if m:
        detail = "ενημερωμένο έως " + m.group(1).strip()
    ocr = "**Text: OCR" in readme
    in_force = next((g for g in (force.groups() if force else ()) if g), "")
    act = Act(
        id=rel, kind="code", title=title, short=CODE_ABBR.get(slug, title),
        consolidation=consolidation, ocr=ocr,
        enacting=enacting.group(1) if enacting else "", in_force=in_force,
    )
    act.note = _note(consolidation, ocr, act.enacting, in_force, detail)
    return act, _units_from_article_files(root, rel, act)


def parse_constitution(root: Path) -> tuple[Act, list[Unit]]:
    act = Act(
        id="constitution", kind="constitution", title="Σύνταγμα της Ελλάδας", short="Σ",
        consolidation="amended",
        note="Ισχύον κείμενο του Συντάγματος, με ενσωματωμένες τις αναθεωρήσεις έως το 2019.",
    )
    return act, _units_from_article_files(root, "constitution", act)


def parse_statute(root: Path, rel: str) -> tuple[Act, list[Unit]] | None:
    """Ένας νόμος ή π.δ.: ``laws/<έτος>/<αριθμός>`` ή ``decrees/<έτος>/<αριθμός>``."""
    folder = root / rel
    kind_dir, year_s, name = rel.split("/")
    kind = "law" if kind_dir == "laws" else "decree"
    amended = name.endswith("-me-tropopoiiseis")
    number = name.replace("-me-tropopoiiseis", "")
    year = int(year_s) if year_s.isdigit() else None
    short = f"{'ν.' if kind == 'law' else 'π.δ.'} {number}/{year_s}"

    source = {}
    if (folder / "source.json").exists():
        try:
            source = json.loads(_read(folder / "source.json"))
        except json.JSONDecodeError:
            source = {}
    readme = _read(folder / "README.md") if (folder / "README.md").exists() else ""
    title = ""
    url = source.get("url") or source.get("source") or ""
    in_force = source.get("in_force") or ""
    if readme:
        first = readme.split("\n", 1)[0].lstrip("# ").strip()
        title = re.sub(r"^(?:ν|π\.δ)\.\s*\d+/\d{4}:\s*", "", first)
        if not url:
            m = _README_URL.search(readme[:3000])
            url = m.group(1) if m else ""
        if not in_force:
            m = _README_FORCE.search(readme[:3000])
            in_force = next((g for g in (m.groups() if m else ()) if g), "")

    has_articles = any(folder.glob("art-*.md"))
    whole = next((n for n in ("text-layer.txt", "text.md") if (folder / n).exists()), None)
    if not has_articles and not whole:
        return None
    ocr = "**Text: OCR" in readme or whole == "text.md"
    act = Act(
        id=rel, kind=kind, title=title, short=short, year=year, number=number,
        consolidation="amended" if amended else "enacted", ocr=ocr,
        official_url=url, in_force=str(in_force or ""), enacting=short,
    )
    if has_articles:
        units = _units_from_article_files(root, rel, act)
    else:
        raw = _read(folder / whole)
        if whole == "text.md":
            raw = _COMMENT.sub("", raw)
            raw = re.sub(r"^# .*\n", "", raw, count=1)
        if not act.title or ocr and len(act.title) < 8:
            act.title = title_from_head(raw) or act.title
        units = _units_from_whole_text(rel, whole, raw, act)
    if not act.title:
        act.title = short
    act.note = _note(act.consolidation, ocr, "" if amended else short, act.in_force)
    return act, units


def list_acts(root: Path) -> list[str]:
    """Όλοι οι φάκελοι πράξεων, με τη σειρά που θα ευρετηριαστούν."""
    rels = ["constitution"]
    rels += [f"codes/{p.name}" for p in sorted((root / "codes").iterdir()) if p.is_dir()]
    for kind in ("laws", "decrees"):
        base = root / kind
        if not base.exists():
            continue
        for year in sorted(p for p in base.iterdir() if p.is_dir()):
            names = {p.name for p in year.iterdir() if p.is_dir()}
            for name in sorted(names):
                # Όπου υπάρχει και η εκδοχή «με τροποποιήσεις», κρατάμε μόνο εκείνη.
                if f"{name}-me-tropopoiiseis" in names:
                    continue
                rels.append(f"{kind}/{year.name}/{name}")
    return rels


def parse_act(root: Path, rel: str) -> tuple[Act, list[Unit]] | None:
    if rel == "constitution":
        return parse_constitution(root)
    if rel.startswith("codes/"):
        return parse_code(root, rel.split("/", 1)[1])
    return parse_statute(root, rel)
