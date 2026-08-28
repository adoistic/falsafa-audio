#!/usr/bin/env python3
"""
Split over-long epics into book/canto-level pseudo-works for generation.

A single 246h Mahābhārata file is unusable as a listening unit and murderous to verify.
Its units carry `ref_start` = "book.chapter.verse" (parva.adhyaya.śloka), so the first
component is the parva. We group by parva -> 18 book-works, each a normal-sized file that
uploads to verse/<slug>/ like any other work and later gets word-level alignment per book.

  python3 pipeline/split_epics.py            # writes /tmp/epic_split.json (list of works)

The book-works are then merged into the verse corpus by build_verse_plus_epics().
"""
from __future__ import annotations
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DATASET = os.path.join(REPO, "dataset")

# The 18 parvas, in order. Diacritics are ASCII-safe here (used only in titles/slugs).
PARVAS = {
    1: "Adi", 2: "Sabha", 3: "Vana", 4: "Virata", 5: "Udyoga", 6: "Bhishma",
    7: "Drona", 8: "Karna", 9: "Shalya", 10: "Sauptika", 11: "Stri", 12: "Shanti",
    13: "Anushasana", 14: "Ashvamedhika", 15: "Ashramavasika", 16: "Mausala",
    17: "Mahaprasthanika", 18: "Svargarohana",
}


def split_mahabharata():
    path = os.path.join(DATASET, "mahabharata.jsonl")
    books = {}                                    # book_no -> list[(unit_idx, text, chars)]
    for line in open(path, encoding="utf-8"):
        d = json.loads(line)
        if d.get("mode") != "song":
            continue
        try:
            book = int(d["ref_start"].split(".")[0])
        except (KeyError, ValueError):
            continue
        books.setdefault(book, []).append(d["text"])
    works = []
    for book in sorted(books):
        units = books[book]
        chars = sum(len(t) for t in units)
        lines = sum(len(t.split("\n")) for t in units)
        blanks = sum(1 for t in units for ln in t.split("\n") if not ln.strip())
        name = PARVAS.get(book, f"Book {book}")
        works.append({
            "slug": f"mahabharata-book-{book:02d}-{name.lower()}",
            "title": f"Mahābhārata — {book}. {name} Parva",
            "author": "Vyāsa", "language": "Sanskrit",
            "units": units, "chars": chars, "lines": lines, "blanks": blanks,
            "epic": "mahabharata", "book": book,       # provenance for the split map
        })
    return works


if __name__ == "__main__":
    works = split_mahabharata()
    json.dump(works, open("/tmp/epic_split.json", "w"), ensure_ascii=False)
    tot = sum(w["chars"] for w in works)
    print(f"mahabharata -> {len(works)} book-works, {tot} chars, ~{tot/1000*83/3600:.0f}h")
    for w in works:
        print(f"  {w['chars']/1000*83/3600:5.1f}h  {w['slug']}")
