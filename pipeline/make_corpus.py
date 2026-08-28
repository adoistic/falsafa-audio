#!/usr/bin/env python3
"""
Build the fleet corpus files that pod_worker.py pulls from R2.

Two corpora, one per voice track:
  narration_corpus.json.gz  prose units of every prose + mixed work   (af_heart)
  verse_corpus.json.gz      song units of every poetic + mixed work   (second voice)

  python3 pipeline/make_corpus.py --mode narration
  python3 pipeline/make_corpus.py --mode verse --upload
"""
from __future__ import annotations
import argparse, gzip, json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DATASET = os.path.join(REPO, "dataset")
CLASSIFICATION = os.path.join(REPO, "classification")

MODES = {
    "narration": ("narration", ("prose", "mixed")),
    "verse":     ("song",      ("poetic", "mixed")),
}


# Works too large to render as a single continuous file — deferred to the chapter-split
# generation leg, where each book/canto becomes its own unit (usable for listening AND
# for word-level read-along). The Mahābhārata alone is ~246h / 10.7M chars.
EXCLUDE = {"mahabharata"}


def build(mode, exclude=EXCLUDE):
    unit_mode, classes = MODES[mode]
    works = {w["slug"]: w for w in json.load(open(f"{CLASSIFICATION}/works.json"))}
    out = []
    for slug, w in works.items():
        if w.get("class") not in classes:
            continue
        if slug in exclude:
            continue
        path = os.path.join(DATASET, f"{slug}.jsonl")
        if not os.path.exists(path):
            continue
        units, chars = [], 0
        for line in open(path, encoding="utf-8"):
            u = json.loads(line)
            if u["mode"] == unit_mode:
                units.append(u["text"])
                chars += len(u["text"])
        if units:
            # verse is synthesised line by line with rests, so its expected duration
            # depends on line counts, not just characters
            lines = sum(len(u.split("\n")) for u in units)
            blanks = sum(1 for u in units for ln in u.split("\n") if not ln.strip())
            out.append({"slug": slug, "title": w["title"], "author": w["author"],
                        "language": w["language"], "units": units, "chars": chars,
                        "lines": lines, "blanks": blanks})
    out.sort(key=lambda x: -x["chars"])          # fat tail first
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=list(MODES), required=True)
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--bucket", default="r2:falsafa-audio")
    args = ap.parse_args()

    works = build(args.mode)
    name = f"{args.mode}_corpus.json.gz"
    path = os.path.join("/tmp", name)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(works, f, ensure_ascii=False)

    chars = sum(w["chars"] for w in works)
    print(f"{args.mode}: {len(works)} works, {sum(len(w['units']) for w in works)} units, "
          f"{chars/1e6:.1f}M chars, ~{chars/1000*61.4/3600:.0f} audio-hours "
          f"-> {path} ({os.path.getsize(path)/1e6:.1f} MB)")

    if args.upload:
        r = subprocess.run(["rclone", "copy", path, f"{args.bucket}/_bootstrap/",
                            "--s3-no-check-bucket"], capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"upload failed: {r.stderr[-400:]}")
        print(f"uploaded to {args.bucket}/_bootstrap/{name}")


if __name__ == "__main__":
    main()
