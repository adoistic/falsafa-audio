#!/usr/bin/env python3
"""
Falsafa-audio dataset builder.

Given a set of work slugs, (re)segments them into dataset/<slug>.jsonl, then rebuilds
dataset/index.json, updates classification/works.json, and refreshes README.md counts.

Verse spans (embedded verse inside prose chapters, from the Phase 3 LLM pass) are read from
classification/verse-spans.json if present:  { "<slug>": { "<chapter_slug>": [[start,end],...] } }

Usage:
  python3 pipeline/build.py <slug> [<slug> ...]     # build specific works
  python3 pipeline/build.py --all                   # rebuild every work in the corpus manifest
  python3 pipeline/build.py --missing               # build only works absent from dataset/
  python3 pipeline/build.py --reindex               # just rebuild index.json + README from jsonl
"""
from __future__ import annotations
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CORPUS = "/Users/siraj/falsafa/corpus"
DATASET = os.path.join(REPO, "dataset")
CLASSIFICATION = os.path.join(REPO, "classification")

sys.path.insert(0, HERE)
from segment import segment_work, load_chapters, load_work_meta  # noqa: E402


def load_manifest():
    m = json.load(open(os.path.join(CORPUS, "manifest.json")))
    return {w["slug"]: w for w in m["works"]}


def load_verse_spans():
    p = os.path.join(CLASSIFICATION, "verse-spans.json")
    return json.load(open(p)) if os.path.exists(p) else {}


def classify(chapters, song_units=None, narration_units=None):
    """Derive chapter layout summary from the corpus chapters, and work class from the
    final unit modes (song/narration) — not raw chapter layout — so a work whose verse is
    only found via internal (sub-chapter) classification still lands in "mixed", not "prose"."""
    layouts = {}
    verse = prose = 0
    for ch in chapters:
        layouts[ch["chapter_slug"]] = ch["layout"]
        if ch["layout"] == "verse":
            verse += 1
        else:
            prose += 1
    if song_units is not None and narration_units is not None:
        has_song, has_narr = song_units > 0, narration_units > 0
    else:
        has_song, has_narr = bool(verse), bool(prose)
    if has_song and has_narr:
        cls = "mixed"
    elif has_song:
        cls = "poetic"
    elif has_narr:
        cls = "prose"
    else:
        cls = "unknown"
    return cls, verse, prose, layouts


def build_work(slug, manifest, verse_spans):
    """Segment one work, write its jsonl, return (index_entry, works_entry)."""
    overrides = None
    if slug in verse_spans:
        overrides = {ch: set(offsets) for ch, offsets in verse_spans[slug].items()}
    units, meta, chapters = segment_work(slug, CORPUS, overrides=overrides)
    # write jsonl
    with open(os.path.join(DATASET, f"{slug}.jsonl"), "w", encoding="utf-8") as f:
        for u in units:
            f.write(json.dumps(u, ensure_ascii=False) + "\n")
    song = sum(1 for u in units if u["mode"] == "song")
    narr = sum(1 for u in units if u["mode"] == "narration")
    cls, verse, prose, layouts = classify(chapters, song, narr)
    mw = manifest.get(slug, {})
    index_entry = {
        "slug": slug,
        "title": meta["title"],
        "language": mw.get("language", meta["language"]),
        "genre": mw.get("genre", meta.get("genre", "Unknown")),
        "class": cls,
        "song_units": song,
        "narration_units": narr,
    }
    works_entry = {
        "slug": slug,
        "title": meta["title"],
        "author": mw.get("author", ""),
        "language": mw.get("language", meta["language"]),
        "genre": mw.get("genre", meta.get("genre", "Unknown")),
        "era": mw.get("era", ""),
        "class": cls,
        "verse_chapters": verse,
        "prose_chapters": prose,
        "total_chapters": verse + prose,
        "chapter_layouts": layouts,
    }
    return index_entry, works_entry


def reindex_and_stats():
    """Rebuild dataset/index.json from the works registry + jsonl unit counts, refresh README."""
    works = {w["slug"]: w for w in json.load(open(os.path.join(CLASSIFICATION, "works.json")))}
    index = []
    for fn in sorted(os.listdir(DATASET)):
        if not fn.endswith(".jsonl"):
            continue
        slug = fn[:-6]
        song = narr = 0
        title = language = None
        for line in open(os.path.join(DATASET, fn), encoding="utf-8"):
            u = json.loads(line)
            title = title or u["title"]
            language = language or u["language"]
            if u["mode"] == "song":
                song += 1
            else:
                narr += 1
        w = works.get(slug, {})
        index.append({
            "slug": slug,
            "title": title or w.get("title", slug),
            "language": language or w.get("language", "English"),
            "genre": w.get("genre", "Unknown"),
            "class": w.get("class", "unknown"),
            "song_units": song,
            "narration_units": narr,
        })
    index.sort(key=lambda x: x["slug"])
    json.dump(index, open(os.path.join(DATASET, "index.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    n_works = len(index)
    n_song = sum(x["song_units"] for x in index)
    n_narr = sum(x["narration_units"] for x in index)
    _update_readme(n_works, n_song, n_narr)
    return n_works, n_song, n_narr


def _update_readme(n_works, n_song, n_narr):
    path = os.path.join(REPO, "README.md")
    txt = open(path, encoding="utf-8").read()
    import re
    txt = re.sub(r"- \*\*[\d,]+\*\* works", f"- **{n_works:,}** works", txt)
    txt = re.sub(r"- \*\*[\d,]+\*\* song units \(verse\)",
                 f"- **{n_song:,}** song units (verse)", txt)
    txt = re.sub(r"- \*\*[\d,]+\*\* narration units \(prose\)",
                 f"- **{n_narr:,}** narration units (prose)", txt)
    open(path, "w", encoding="utf-8").write(txt)


def main():
    args = sys.argv[1:]
    manifest = load_manifest()
    verse_spans = load_verse_spans()

    if args == ["--reindex"]:
        print("reindex:", reindex_and_stats())
        return

    if args and args[0] == "--all":
        slugs = list(manifest.keys())
    elif args and args[0] == "--missing":
        have = {fn[:-6] for fn in os.listdir(DATASET) if fn.endswith(".jsonl")}
        slugs = [s for s in manifest if s not in have]
    else:
        slugs = args
    if not slugs:
        print("no slugs to build"); return

    works = {w["slug"]: w for w in json.load(open(os.path.join(CLASSIFICATION, "works.json")))}
    built = 0
    for slug in slugs:
        try:
            ie, we = build_work(slug, manifest, verse_spans)
        except Exception as e:
            print(f"ERR {slug}: {e}"); continue
        works[slug] = we
        built += 1
        if built % 25 == 0:
            print(f"  built {built}/{len(slugs)}")
    # persist works.json (sorted by slug)
    out = sorted(works.values(), key=lambda x: x["slug"])
    json.dump(out, open(os.path.join(CLASSIFICATION, "works.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"built {built} works")
    print("reindex:", reindex_and_stats())


if __name__ == "__main__":
    main()
