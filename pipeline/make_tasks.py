#!/usr/bin/env python3
"""
Build per-work verse-classification task files from candidates.json.

For each candidate work, emits classification/verse-tasks/<slug>.json:
  {
    "slug": ..., "title": ...,
    "chapters": [
      { "chapter_slug": ..., "paragraphs": [ {"i": 0, "offset": 0, "text": "..."} , ... ] },
      ...
    ]
  }

A subagent reads this, decides which paragraphs are embedded VERSE, and writes
classification/verse-spans/<slug>.json = { "<chapter_slug>": [<verse paragraph offsets>], ... }.

Idempotent: skips a task file if it already exists (unless --force).
"""
from __future__ import annotations
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CORPUS = "/Users/siraj/falsafa/corpus"
TASKS = os.path.join(REPO, "classification/verse-tasks")

sys.path.insert(0, HERE)
from segment import _read_frontmatter_and_body, _paragraphs_from_body  # noqa: E402


def paragraphs_for(chdir, variant):
    pbase = variant.rsplit(".md", 1)[0]
    ppath = os.path.join(chdir, f"{pbase}.paragraphs.json")
    if os.path.exists(ppath):
        return json.load(open(ppath))
    md = os.path.join(chdir, variant)
    if os.path.exists(md):
        return _paragraphs_from_body(_read_frontmatter_and_body(md))
    return []


def main():
    force = "--force" in sys.argv
    os.makedirs(TASKS, exist_ok=True)
    cands = json.load(open(os.path.join(REPO, "classification/candidates.json")))
    works = {w["slug"]: w for w in json.load(open(os.path.join(REPO, "classification/works.json")))}
    by_work = {}
    for c in cands:
        by_work.setdefault(c["slug"], []).append(c)
    written = skipped = 0
    for slug, chs in by_work.items():
        out = os.path.join(TASKS, f"{slug}.json")
        if os.path.exists(out) and not force:
            skipped += 1
            continue
        chapters = []
        for c in sorted(chs, key=lambda x: x["chapter_dir"]):
            chdir = os.path.join(CORPUS, "works", slug, "chapters", c["chapter_dir"])
            paras = paragraphs_for(chdir, c["variant"])
            chapters.append({
                "chapter_slug": c["chapter_slug"],
                "paragraphs": [{"i": i, "offset": p["offset"], "text": p["text"]}
                               for i, p in enumerate(paras)],
            })
        task = {"slug": slug, "title": works.get(slug, {}).get("title", slug), "chapters": chapters}
        json.dump(task, open(out, "w", encoding="utf-8"), ensure_ascii=False)
        written += 1
    print(f"tasks written: {written}  skipped(existing): {skipped}  total works: {len(by_work)}")


if __name__ == "__main__":
    main()
