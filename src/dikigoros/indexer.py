"""Χτίζει το ευρετήριο SQLite από ένα checkout του lexifina/greece-law.

    python -m dikigoros.indexer --source /path/to/greece-law --out data/index.db

Πίνακες: ``acts`` (πράξεις), ``units`` (άρθρα, κείμενο συμπιεσμένο με zlib) και
``units_fts`` (FTS5 πάνω στα θέματα των λέξεων, βλ. ``greek.py``).
"""

from __future__ import annotations

import argparse
import datetime as dt
import multiprocessing as mp
import os
import sqlite3
import subprocess
import sys
import time
import zlib
from pathlib import Path

from .corpus import list_acts, parse_act
from .greek import index_text

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE acts (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT, short TEXT, year INTEGER, number TEXT,
    consolidation TEXT, ocr INTEGER, note TEXT, official_url TEXT, in_force TEXT, enacting TEXT,
    n_units INTEGER DEFAULT 0
);
CREATE TABLE units (
    id INTEGER PRIMARY KEY, uid TEXT UNIQUE NOT NULL, act_id TEXT NOT NULL, kind TEXT NOT NULL,
    label TEXT, num INTEGER, suffix TEXT, part INTEGER, heading TEXT, context TEXT,
    body BLOB, chars INTEGER, path TEXT, ocr INTEGER
);
CREATE INDEX units_act ON units(act_id, num, suffix, part);
CREATE VIRTUAL TABLE units_fts USING fts5(head, body, content='', tokenize='unicode61');
CREATE VIRTUAL TABLE acts_fts USING fts5(title, content='', tokenize='unicode61');
"""

_ROOT: Path | None = None


def _init(root: str) -> None:
    global _ROOT
    _ROOT = Path(root)


def _work(rel: str):
    """Διαβάζει μία πράξη και ετοιμάζει τις γραμμές της (τρέχει σε ξεχωριστή διεργασία)."""
    try:
        parsed = parse_act(_ROOT, rel)
    except Exception as exc:  # μία χαλασμένη πράξη δεν σταματά το χτίσιμο
        return rel, None, f"{type(exc).__name__}: {exc}"
    if not parsed:
        return rel, None, "κενός φάκελος"
    act, units = parsed
    rows = []
    for u in units:
        # Ο τίτλος της πράξης και το πλαίσιο μπαίνουν στην «κεφαλή», ώστε να μετρούν περισσότερο.
        head = index_text(" ".join((u.heading, u.context, act.title if act.kind != "code" else "")))
        rows.append((u.uid, u.act_id, act.kind, u.label, u.num, u.suffix, u.part, u.heading, u.context,
                     zlib.compress(u.text.encode("utf-8"), 6), len(u.text), u.path, int(u.ocr),
                     head, index_text(u.text)))
    return rel, (act.as_dict(), rows), None


def source_commit(root: Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                             text=True, timeout=20)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def build(source: Path, out: Path, workers: int = 0, only: list[str] | None = None,
          quiet: bool = False) -> dict:
    """Χτίζει το ευρετήριο σε προσωρινό αρχείο και το μετονομάζει ατομικά στο τέλος."""
    rels = list_acts(source)
    if only:
        rels = [r for r in rels if any(r == o or r.startswith(o.rstrip("/") + "/") for o in only)]
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".building")
    for stale in (tmp, Path(str(tmp) + "-journal")):
        if stale.exists():
            stale.unlink()
    db = sqlite3.connect(tmp)
    db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;" + SCHEMA)
    t0 = time.time()
    stats = {"acts": 0, "units": 0, "chars": 0, "skipped": 0, "errors": []}
    workers = workers or max(1, (os.cpu_count() or 2))

    def store(result) -> None:
        rel, payload, error = result
        if error or not payload:
            stats["skipped"] += 1
            if error and error != "κενός φάκελος":
                stats["errors"].append(f"{rel}: {error}")
            return
        act, rows = payload
        if not rows:
            stats["skipped"] += 1
            return
        db.execute(
            "INSERT INTO acts (id, kind, title, short, year, number, consolidation, ocr, note,"
            " official_url, in_force, enacting, n_units) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (act["id"], act["kind"], act["title"], act["short"], act["year"], act["number"],
             act["consolidation"], int(act["ocr"]), act["note"], act["official_url"], act["in_force"],
             act["enacting"], len(rows)),
        )
        rowid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO acts_fts (rowid, title) VALUES (?, ?)",
                   (rowid, index_text(f"{act['title']} {act['short']}")))
        for row in rows:
            cur = db.execute(
                "INSERT OR IGNORE INTO units (uid, act_id, kind, label, num, suffix, part, heading,"
                " context, body, chars, path, ocr) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", row[:13])
            if cur.rowcount:
                db.execute("INSERT INTO units_fts (rowid, head, body) VALUES (?,?,?)",
                           (cur.lastrowid, row[13], row[14]))
                stats["units"] += 1
                stats["chars"] += row[10]
        stats["acts"] += 1

    if workers == 1:
        _init(str(source))
        for n, rel in enumerate(rels, 1):
            store(_work(rel))
    else:
        with mp.Pool(workers, initializer=_init, initargs=(str(source),)) as pool:
            for n, result in enumerate(pool.imap(_work, rels, chunksize=8), 1):
                store(result)
                if not quiet and n % 500 == 0:
                    print(f"  {n}/{len(rels)} πράξεις, {stats['units']} μονάδες, {time.time() - t0:.0f}s",
                          file=sys.stderr, flush=True)

    meta = {
        "built_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "https://github.com/lexifina/greece-law",
        "source_commit": source_commit(source),
        "acts": str(stats["acts"]), "units": str(stats["units"]), "chars": str(stats["chars"]),
    }
    db.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
    db.commit()
    db.execute("INSERT INTO units_fts(units_fts) VALUES ('optimize')")
    db.commit()
    db.execute("VACUUM")
    db.close()
    os.replace(tmp, out)
    stats["seconds"] = round(time.time() - t0, 1)
    stats["size_mb"] = round(out.stat().st_size / 1e6, 1)
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--source", required=True, type=Path, help="checkout του lexifina/greece-law")
    ap.add_argument("--out", default=Path("data/index.db"), type=Path)
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--only", nargs="*", help="μόνο αυτοί οι φάκελοι (π.χ. constitution codes)")
    args = ap.parse_args(argv)
    if not (args.source / "codes").is_dir():
        print(f"Δεν βρέθηκε ο φάκελος codes/ στο {args.source}", file=sys.stderr)
        return 2
    stats = build(args.source, args.out, args.workers, args.only)
    errors = stats.pop("errors")
    print(stats)
    for line in errors[:20]:
        print("  σφάλμα:", line, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
