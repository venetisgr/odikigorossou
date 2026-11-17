"""Ελληνική κανονικοποίηση για την αναζήτηση: αποτονισμός, τελικό σίγμα, ελαφρύ stemming.

Το ευρετήριο και τα ερωτήματα περνούν από τις ίδιες συναρτήσεις, ώστε
«προθεσμία», «προθεσμίες» και «προθεσμιών» να καταλήγουν στο ίδιο θέμα.
Δεν είναι γλωσσολογικά πλήρης stemmer· κόβει μόνο κλιτικές καταλήξεις.
"""

from __future__ import annotations

import re
import unicodedata

_MARKS = re.compile("[̀-ͯ]")
_TOKEN = re.compile(r"[0-9a-zα-ω]+")
_HAS_GREEK = re.compile(r"[α-ω]")
_HAS_LATIN = re.compile(r"[a-z]")

# Λατινικοί χαρακτήρες που μοιάζουν με ελληνικούς (συχνό σε OCR και σε τίτλους ΦΕΚ: «NOMOΣ»).
_LOOKALIKES = str.maketrans(
    {"a": "α", "b": "β", "e": "ε", "h": "η", "i": "ι", "k": "κ", "m": "μ", "n": "ν",
     "o": "ο", "p": "ρ", "t": "τ", "x": "χ", "y": "υ", "z": "ζ", "v": "ν", "u": "υ"}
)

STOPWORDS = frozenset(
    """
    ο η το οι τα του της των τον την τους τις τη στο στη στην στον στου στης στων στους στις στα
    και κι να με σε για απο προς κατα μετα παρα αντι δια επι υπο περι ως εως χωρις μεχρι
    που πως οτι οταν οπου οπως αν εαν ειτε ουτε μητε αλλα ομως ενω αφου επειδη διοτι γιατι
    θα δεν μη μην ειναι ηταν εχει εχουν ειχε εχω ειμαι εισαι
    ενας ενα μια μιας ενος αυτος αυτη αυτο αυτοι αυτες αυτα αυτου αυτης αυτων αυτον αυτην
    ποιος ποια ποιο ποιοι ποιες ποιον τι ποτε ποσο ποσα ποση
    οποιος οποια οποιο οποιοι οποιες οποιου οποιας οποιων οποιον οποιαν
    εγω εσυ μου σου μας σας εμεις εσεις τουτο εκεινος εκεινη εκεινο
    πιο πολυ λιγο καθε ολα ολοι ολες ολος ολη ολο αλλος αλλη αλλο
    υπαρχει υπαρχουν μπορει μπορω μπορουν πρεπει ισχυει ισχυουν
    εν εκ εξ συν δε γε τε ηδη ακομη επισης μονο καθως ωστε
    παρ περ εδ αρθρ πδ φεκ αρθρο αρθρου αρθρα αρθρων αρθρον παραγραφος παραγραφου παραγραφο
    """.replace("ς", "σ").split()  # οι λέξεις συγκρίνονται μετά το fold, όπου δεν υπάρχει τελικό σίγμα
)

# Κλιτικές καταλήξεις, από τη μακρύτερη στη βραχύτερη. Το θέμα κρατά τουλάχιστον 3 γράμματα.
_SUFFIXES = (
    "ουνται", "ονται", "ειται", "ουμε", "ατων", "ατος",
    "εται", "ειας", "ειες", "ειων", "ουν", "ατα", "εων", "εως", "εις", "ους", "εια",
    "ιας", "ιες", "ιων", "ιου",
    "ων", "ες", "ος", "ου", "ης", "ας", "ον", "ην", "ει", "αν", "ια", "ιο",
    "οι", "α", "η", "ο", "ι", "ε", "υ", "ω",
)
_SUFFIXES = tuple(s.replace("ς", "σ") for s in _SUFFIXES)  # μετά το fold δεν υπάρχει τελικό σίγμα
_M_ONLY = frozenset(("ατων", "ατοσ", "ατα"))
_MIN_STEM = 3
_stem_cache: dict[str, str] = {}


def fold(text: str) -> str:
    """Πεζά, χωρίς τόνους/πνεύματα/διαλυτικά, με «ς» → «σ»."""
    text = unicodedata.normalize("NFD", text)
    text = _MARKS.sub("", text).lower()
    return text.replace("ς", "σ")


def stem(token: str) -> str:
    """Θέμα μιας ήδη κανονικοποιημένης (fold) λέξης."""
    cached = _stem_cache.get(token)
    if cached is not None:
        return cached
    out = token
    if not token.isdigit() and len(token) > _MIN_STEM and _HAS_GREEK.search(token):
        for suffix in _SUFFIXES:
            if token.endswith(suffix) and len(token) - len(suffix) >= _MIN_STEM:
                # «δικαιώματος» → «δικαιωμ» (όχι «δικαιω»): οι -ατος/-ατα/-ατων κόβονται μόνο μετά από «μ».
                if suffix in _M_ONLY and not token[: -len(suffix)].endswith("μ"):
                    continue
                out = token[: -len(suffix)]
                break
    if len(_stem_cache) < 2_000_000:
        _stem_cache[token] = out
    return out


def tokens(text: str) -> list[str]:
    """Κανονικοποιημένες λέξεις (χωρίς stemming, με τα stopwords)."""
    out = []
    for tok in _TOKEN.findall(fold(text)):
        if _HAS_LATIN.search(tok) and _HAS_GREEK.search(tok):
            tok = tok.translate(_LOOKALIKES)
        out.append(tok)
    return out


def stems(text: str, keep_stopwords: bool = False) -> list[str]:
    """Τα θέματα ενός κειμένου, με τη σειρά που εμφανίζονται."""
    out = []
    for tok in tokens(text):
        if len(tok) < 2 and not tok.isdigit():
            continue
        if not keep_stopwords and tok in STOPWORDS:
            continue
        out.append(stem(tok))
    return out


def index_text(text: str) -> str:
    """Το κείμενο όπως αποθηκεύεται στο FTS: θέματα χωρισμένα με κενό."""
    return " ".join(stems(text))


_ORDINALS = {
    "πρωτο": 1, "δευτερο": 2, "τριτο": 3, "τεταρτο": 4, "πεμπτο": 5, "εκτο": 6, "εβδομο": 7,
    "ογδοο": 8, "ενατο": 9, "δεκατο": 10, "ενδεκατο": 11, "δωδεκατο": 12, "μονο": 1,
    "μονον": 1, "πρωτον": 1, "δευτερον": 2, "τριτον": 3,
}
_ARTICLE_LINE = re.compile(r"^αρθρον?\s+(\d{1,4})\s*([α-ω]{0,2})\s*[.:]?$")
_ARTICLE_ORDINAL = re.compile(r"^αρθρον?\s+([α-ω]+)\s*[.:]?$")


def article_heading(line: str) -> tuple[int, str] | None:
    """(αριθμός, γράμμα) αν η γραμμή είναι μόνη της επικεφαλίδα άρθρου («Άρθρο 5», «Άρθρο 5Α», «Άρθρο πρώτο»)."""
    folded = fold(line).strip(" \t᾿῾῎῞'’`´.-–—·*#_")
    if not folded.startswith("αρθρο") or len(folded) > 24:
        return None
    folded = folded.translate(_LOOKALIKES) if _HAS_LATIN.search(folded) else folded
    m = _ARTICLE_LINE.match(folded)
    if m:
        return int(m.group(1)), m.group(2).upper()
    m = _ARTICLE_ORDINAL.match(folded)
    if m and m.group(1) in _ORDINALS:
        return _ORDINALS[m.group(1)], ""
    return None
