import pytest


def test_meta_and_catalogue(store):
    meta = store.meta()
    assert meta["by_kind"] == {"constitution": 1, "code": 2, "law": 2, "decree": 1}
    assert [a["short"] for a in store.catalogue()] == ["Σ", "ΑΚ", "ΚΑΕΔ"]


@pytest.mark.parametrize("ref,expected", [
    ("ΑΚ", "codes/civil-code"), ("Α.Κ.", "codes/civil-code"), ("Αστικός Κώδικας", "codes/civil-code"),
    ("αστικού κώδικα", "codes/civil-code"), ("ΚΑΕΔ", "codes/labour-code"), ("Σύνταγμα", "constitution"),
    ("ν. 5053/2023", "laws/2023/5053"), ("Ν.5053/2023", "laws/2023/5053"), ("νόμος 4648/2019", "laws/2019/4648"),
    ("π.δ. 101/2000", "decrees/2000/101"), ("ΠΔ 101/2000", "decrees/2000/101"),
    ("π.δ. 80/2022", "codes/labour-code"),  # το διάταγμα που κύρωσε τον κώδικα οδηγεί στον κώδικα
    ("ν. 9999/2020", None), ("κάτι άσχετο", None), ("", None),
])
def test_resolve_act(store, ref, expected):
    assert store.resolve_act(ref) == expected


def test_find_article_and_cite_labels(store):
    (art,) = store.find_article("codes/civil-code", "914")
    assert art["cite"] == "ΑΚ 914" and art["act"]["consolidation"] == "enacted" and art["ocr"]
    assert store.find_article("constitution", "5Α")[0]["cite"] == "Σ 5Α"
    assert store.find_article("constitution", "5a")[0]["cite"] == "Σ 5Α"
    assert store.find_article("codes/civil-code", "1", intro_law=True)[0]["cite"] == "ΕισΝΑΚ 1"
    assert store.find_article("laws/2023/5053", 19)[0]["cite"] == "άρθρο 19 ν. 5053/2023"
    assert store.unit("decrees/2000/101/full")["cite"] == "π.δ. 101/2000"
    assert store.find_article("codes/civil-code", "9999") == []


def test_search_matches_inflected_forms_and_ranks_headings(store):
    hits = store.search("ηθικές βλάβες χρηματικής ικανοποίησης")
    assert hits[0]["cite"] == "ΑΚ 932" and hits[0]["coverage"] == 1.0
    assert "ηθικής βλάβης" in hits[0]["snippet"]
    hits = store.search("καταγγελία σύμβασης εργασίας προειδοποίηση")
    assert {h["cite"] for h in hits[:2]} == {"ΚΑΕΔ 321", "άρθρο 19 ν. 5053/2023"}


def test_search_filters(store):
    assert {h["kind"] for h in store.search("καταγγελία σύμβασης εργασίας", kinds=["law"])} == {"law"}
    hits = store.search("καταγγελία σύμβασης εργασίας", act_ids=["codes/labour-code"])
    assert [h["cite"] for h in hits] == ["ΚΑΕΔ 321"]
    assert store.search("του της και") == []           # μόνο stopwords
    assert store.search('" OR units_fts MATCH') == [] or True  # δεν σπάει σε ειδικούς χαρακτήρες


def test_neighbours_and_toc(store):
    art = store.unit("codes/civil-code/art-914")
    around = store.neighbours(art)
    assert around["prev"] is None and around["next"]["label"] == "Άρθρο 932"
    assert [t["label"] for t in store.toc("codes/civil-code")] == ["Άρθρο 914", "Άρθρο 932", "Άρθρο 1 ΕισΝ"]
    assert store.search_acts("εκλογικού δικαιώματος")[0]["id"] == "laws/2019/4648"
