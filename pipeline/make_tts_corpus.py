#!/usr/bin/env python3
"""
Build the REGENERATION corpora for the Indic works: spoken text for TTS +
alignment, and the token maps that tie spoken words back to display words.

Outputs (affected works only):
  /tmp/narration_corpus.json.gz   spoken narration units  (pod_worker + align_worker)
  /tmp/verse_corpus.json.gz       spoken song units, Mahābhārata split into
                                  its 18 parva streams exactly like split_epics
  out/tts_maps.json.gz            {"<track>:<slug>": [[disp_idx,...], ...]}
                                  one list per unit, len == spoken token count
                                  (align_merge uses this; also uploaded to
                                  _bootstrap for the record)

  python3 make_tts_corpus.py --rules tts_rules.json --affected /tmp/tts_affected.json [--upload]

The spoken corpora REPLACE _bootstrap/{narration,verse}_corpus.json.gz for the
regen wave: pod_worker regenerates these works' audio from spoken text, and
align_worker aligns the same spoken text against the new audio. Display text
(dataset/, corpus/) is untouched.
"""
from __future__ import annotations
import argparse, gzip, json, os, subprocess, sys

from split_epics import PARVAS
from tts_normalize import spoken_unit

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DATASET = os.path.join(REPO, "dataset")
OUT = os.path.join(REPO, "out")

MODES = {"narration": "narration", "verse": "song"}


def build(rules, affected):
    works_meta = {w["slug"]: w for w in
                  json.load(open(os.path.join(REPO, "classification", "works.json")))}
    corpora = {"narration": [], "verse": []}
    maps = {}

    for slug in affected:
        flags = rules.get(slug)
        if flags is None:
            print(f"  !! no rules for {slug}, skipping"); continue
        path = os.path.join(DATASET, f"{slug}.jsonl")
        units = [json.loads(l) for l in open(path, encoding="utf-8")]
        meta = works_meta.get(slug, {"title": slug, "author": "", "language": ""})

        if slug == "mahabharata":
            # parva streams, same grouping as split_epics (song units by book)
            by_book = {}
            for u in units:
                if u["mode"] != "song":
                    continue
                try:
                    book = int(u["ref_start"].split(".")[0])
                except (KeyError, ValueError):
                    continue
                by_book.setdefault(book, []).append(u)
            for book in sorted(by_book):
                name = PARVAS.get(book)
                if not name:
                    continue
                pslug = f"mahabharata-book-{book:02d}-{name.lower()}"
                sp_units, sp_maps = [], []
                for u in by_book[book]:
                    sp, m = spoken_unit(u["text"], flags)
                    sp_units.append(sp); sp_maps.append(m)
                corpora["verse"].append(_entry(pslug, meta, sp_units))
                maps[f"verse:{pslug}"] = sp_maps
            continue

        for track, mode in MODES.items():
            tr_units = [u for u in units if u["mode"] == mode]
            if not tr_units:
                continue
            sp_units, sp_maps = [], []
            for u in tr_units:
                sp, m = spoken_unit(u["text"], flags)
                sp_units.append(sp); sp_maps.append(m)
            corpora[track].append(_entry(slug, meta, sp_units))
            maps[f"{track}:{slug}"] = sp_maps
    return corpora, maps


def _entry(slug, meta, units):
    chars = sum(len(u) for u in units)
    lines = sum(len(u.split("\n")) for u in units)
    blanks = sum(1 for u in units for ln in u.split("\n") if not ln.strip())
    return {"slug": slug, "title": meta.get("title", slug),
            "author": meta.get("author", ""), "language": meta.get("language", ""),
            "units": units, "chars": chars, "lines": lines, "blanks": blanks}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules", default=os.path.join(HERE, "tts_rules.json"))
    ap.add_argument("--affected", default="/tmp/tts_affected.json")
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--bucket", default="r2:falsafa-audio")
    args = ap.parse_args()

    rules = json.load(open(args.rules, encoding="utf-8"))
    affected = json.load(open(args.affected, encoding="utf-8"))
    corpora, maps = build(rules, affected)

    os.makedirs(OUT, exist_ok=True)
    for track, works in corpora.items():
        works.sort(key=lambda x: -x["chars"])
        p = f"/tmp/{track}_corpus.json.gz"
        with gzip.open(p, "wt", encoding="utf-8") as f:
            json.dump(works, f, ensure_ascii=False)
        chars = sum(w["chars"] for w in works)
        print(f"{track}: {len(works)} streams, {chars/1e6:.1f}M spoken chars -> {p}")

    mp = os.path.join(OUT, "tts_maps.json.gz")
    with gzip.open(mp, "wt", encoding="utf-8") as f:
        json.dump(maps, f)
    print(f"maps: {len(maps)} streams -> {mp}")

    if args.upload:
        for p in ("/tmp/narration_corpus.json.gz", "/tmp/verse_corpus.json.gz", mp):
            r = subprocess.run(["rclone", "copy", p, f"{args.bucket}/_bootstrap/",
                                "--s3-no-check-bucket"], capture_output=True, text=True)
            if r.returncode != 0:
                sys.exit(f"upload failed for {p}: {r.stderr[-300:]}")
        print("uploaded to _bootstrap/ (REPLACES the full corpora — regen wave only)")


if __name__ == "__main__":
    main()
