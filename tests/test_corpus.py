from dikigoros.corpus import (label_from_filename, list_acts, parse_act, parse_article_file, reflow,
                              split_articles, split_parts, title_from_head)

from conftest import FIXTURES


def by_label(units):
    return {u.label: u for u in units}


def test_list_acts_orders_constitution_codes_laws_decrees():
    assert list_acts(FIXTURES) == ["constitution", "codes/civil-code", "codes/labour-code",
                                   "laws/2019/4648", "laws/2023/5053", "decrees/2000/101"]


def test_me_tropopoiiseis_folder_replaces_the_enacted_copy(tmp_path):
    for name in ("4654", "4654-me-tropopoiiseis", "4660"):
        (tmp_path / "laws" / "2020" / name).mkdir(parents=True)
    (tmp_path / "codes").mkdir()
    assert list_acts(tmp_path) == ["constitution", "laws/2020/4654-me-tropopoiiseis", "laws/2020/4660"]


def test_code_article_with_ocr_marker_context_and_inline_heading():
    act, units = parse_act(FIXTURES, "codes/civil-code")
    assert (act.short, act.consolidation, act.ocr, act.enacting, act.in_force) == \
        ("ΑΚ", "enacted", True, "π.δ. 456/1984", "1984-10-24")
    assert "δεν ενσωματώνει μεταγενέστερες τροποποιήσεις" in act.note and "OCR" in act.note
    art = by_label(units)["Άρθρο 914"]
    assert art.heading == "Έννοια" and art.ocr
    assert art.context == "ΚΕΦΑΛΑΙΟ ΤΡΙΑΚΟΣΤΟ ΕΝΑΤΟ · ΑΔΙΚΟΠΡΑΞΙΕΣ"
    assert art.text.startswith("Όποιος ζημιώσει άλλον")
    assert art.path == "greece/national/acts/codes/civil-code/art-914.md"
    assert by_label(units)["Άρθρο 1 ΕισΝ"].num == 50_001


def test_code_article_joins_paragraphs_split_mid_sentence():
    _, units = parse_act(FIXTURES, "codes/labour-code")
    art = by_label(units)["Άρθρο 321"]
    assert art.heading == "Καταγγελία με προειδοποίηση από τον εργοδότη"
    assert "έως δύο (2) έτη, απαιτείται" in art.text
    assert "\n2. Εργοδότης" in art.text  # οι αριθμημένες παράγραφοι μένουν χωριστές


def test_law_article_file_keeps_quoted_article_in_body():
    act, units = parse_act(FIXTURES, "laws/2023/5053")
    assert (act.kind, act.short, act.in_force) == ("law", "ν. 5053/2023", "2023-09-26")
    assert act.official_url.endswith("20230100158.pdf")
    (art,) = units
    assert art.label == "Άρθρο 19" and art.heading.startswith("Καταγγελία σύμβασης εργασίας")
    assert art.context.startswith("ΜΕΡΟΣ Γ’") and "«Άρθρο 325Α" in art.text


def test_whole_gazette_text_is_split_into_articles():
    act, units = parse_act(FIXTURES, "laws/2019/4648")
    assert act.title == "Διευκόλυνση άσκησης εκλογικού δικαιώματος εκλογέων που βρίσκονται εκτός Ελληνικής Επικράτειας"
    assert [u.label for u in units] == ["Άρθρο 1", "Άρθρο 2", "Άρθρο 3"]
    one, two, _ = units
    assert one.heading == "Πεδίο εφαρμογής – Ορισμοί – Χρόνος διεξαγωγής ψηφοφορίας"
    assert "βουλευτικών εκλογών βρίσκονται" in one.text      # συλλαβισμός στο τέλος γραμμής
    assert "4520" not in one.text                             # αριθμός σελίδας
    assert "Άρθρο 5" in two.text                              # παρατιθέμενο άρθρο άλλου νόμου
    assert one.uid == "laws/2019/4648/art-1" and one.path.endswith("laws/2019/4648/text-layer.txt")


def test_text_without_articles_becomes_one_unit():
    act, units = parse_act(FIXTURES, "decrees/2000/101")
    assert act.short == "π.δ. 101/2000" and act.title.startswith("Σύσταση Ειδικής Υπηρεσίας")
    assert [u.label for u in units] == ["Πλήρες κείμενο"]


def test_split_articles_needs_a_running_sequence():
    assert split_articles("Άρθρο 7\nκάτι\nΆρθρο 3\nάλλο") == []
    arts = split_articles("Άρθρο 1\nα\nΆρθρο 2\nβ\nΆρθρο 9\nγ\nΆρθρο 3\nδ")
    assert [a["num"] for a in arts] == [1, 2, 3]


def test_reflow_and_title_and_parts():
    assert reflow(["Η προθε-", "σμία είναι", "δέκα ημέρες.", "2. Άλλο."]) == "Η προθεσμία είναι δέκα ημέρες.\n2. Άλλο."
    assert title_from_head("ΝΟΜΟΣ ΥΠ’ ΑΡΙΘ. 10\nΠερί κάτι\nκαι άλλου.\nΟ ΠΡΟΕΔΡΟΣ\n") == "Περί κάτι και άλλου"
    parts = split_parts("\n".join(["λέξη " * 40] * 30), limit=1000)
    assert len(parts) > 1 and all(len(p) <= 1000 for p in parts)
    assert "".join(parts).replace("\n", "") == ("λέξη " * 40 * 30)


def test_label_from_filename_and_plain_article_file():
    assert label_from_filename("art-005-a") == ("Άρθρο 5Α", 5, "Α")
    assert label_from_filename("intro-law-art-012")[0] == "Άρθρο 12 ΕισΝ"
    assert label_from_filename("prooimio")[0] == "Προοίμιο"
    parsed = parse_article_file("# Άρθρο 299\n\nΑνθρωποκτονία με δόλο 1. Όποιος σκότωσε άλλον τιμωρείται.", "code")
    assert parsed["heading"] == "Ανθρωποκτονία με δόλο" and parsed["text"].startswith("1. Όποιος")
