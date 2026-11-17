import pytest

from dikigoros.greek import article_heading, fold, index_text, stems


def test_fold_strips_accents_and_final_sigma():
    assert fold("Προθεσμίες ΆΣΚΗΣΗΣ") == "προθεσμιεσ ασκησησ"
    assert fold("ἐν τῷ νόμῳ") == "εν τω νομω"  # πολυτονικό


@pytest.mark.parametrize("forms", [
    "προθεσμία προθεσμίες προθεσμιών προθεσμίας",
    "σύμβαση σύμβασης συμβάσεις συμβάσεων συμβάσεως",
    "δικαίωμα δικαιώματος δικαιώματα δικαιωμάτων",
    "κληρονόμος κληρονόμου κληρονόμοι κληρονόμων",
    "εργοδότης εργοδότη εργοδοτών",
    "ελευθερία ελευθερίας ελεύθερος ελεύθερη",
    "δικαστήριο δικαστηρίου δικαστήρια δικαστηρίων",
])
def test_inflected_forms_share_a_stem(forms):
    assert len(set(stems(forms))) == 1


def test_stopwords_and_latin_lookalikes():
    assert stems("η καταγγελία της σύμβασης") == ["καταγγελ", "συμβασ"]
    assert stems("NOMOΣ") == stems("ΝΟΜΟΣ")  # λατινικά N, O, M μέσα σε ελληνική λέξη
    assert index_text("το άρθρο 914 του Αστικού Κώδικα") == "914 αστικ κωδικ"


@pytest.mark.parametrize("line,expected", [
    ("Άρθρο 5", (5, "")), ("Άρθρο 5Α", (5, "Α")), ("ΑΡΘΡΟ 12.", (12, "")), ("Άρθρο πρώτο", (1, "")),
    ("῎Αρθρον 14.", (14, "")), ("Άρθρο 5 του ν. 4624/2019", None), ("Το άρθρο 5", None),
])
def test_article_heading(line, expected):
    assert article_heading(line) == expected
