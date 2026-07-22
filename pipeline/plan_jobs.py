#!/usr/bin/env python3
"""
Plan the Phase-3 verse-classification grind as bounded CHUNK jobs.

Each candidate chapter's paragraphs are split into chunks of <= MAX_CHARS. Every chunk becomes
one job file: classification/verse-jobs/<jobid>.txt with labeled paragraphs the subagent reads.
A subagent writes classification/verse-spans/<jobid>.json = {"verse_offsets": [ints]}.

jobid = "<slug>__<chapter_slug>__p<partIndex>"  (filesystem-safe; chapter_slug kept verbatim).

Outputs:
  classification/verse-jobs/*.txt         one per chunk
  classification/verse-jobs/manifest.json [{jobid, slug, chapter_slug, part, n_paras, chars}]
"""
from __future__ import annotations
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TASKS = os.path.join(REPO, "classification/verse-tasks")
JOBS = os.path.join(REPO, "classification/verse-jobs")
MAX_CHARS = 110_000


def chunk_paragraphs(paras):
    """Yield lists of paragraphs, each list's text total <= MAX_CHARS (single oversize para
    still forms its own chunk)."""
    cur, total = [], 0
    for p in paras:
        plen = len(p["text"])
        if cur and total + plen > MAX_CHARS:
            yield cur
            cur, total = [], 0
        cur.append(p)
        total += plen
    if cur:
        yield cur


def render_chunk(title, slug, chapter_slug, paras):
    lines = [f"WORK: {title}  (slug: {slug})", f"CHAPTER: {chapter_slug}", ""]
    for p in paras:
        txt = " ".join(p["text"].split())
        lines.append(f"[{p['offset']}] {txt}")
    return "\n\n".join(lines)


def main():
    os.makedirs(JOBS, exist_ok=True)
    manifest = []
    for fn in sorted(os.listdir(TASKS)):
        if not fn.endswith(".json"):
            continue
        t = json.load(open(os.path.join(TASKS, fn)))
        for ch in t["chapters"]:
            if not ch["paragraphs"]:
                continue
            for part, group in enumerate(chunk_paragraphs(ch["paragraphs"])):
                jobid = f"{t['slug']}__{ch['chapter_slug']}__p{part}"
                path = os.path.join(JOBS, f"{jobid}.txt")
                open(path, "w", encoding="utf-8").write(
                    render_chunk(t["title"], t["slug"], ch["chapter_slug"], group))
                manifest.append({
                    "jobid": jobid,
                    "slug": t["slug"],
                    "chapter_slug": ch["chapter_slug"],
                    "part": part,
                    "n_paras": len(group),
                    "chars": sum(len(p["text"]) for p in group),
                })
    json.dump(manifest, open(os.path.join(JOBS, "manifest.json"), "w"), indent=1)
    total_chars = sum(m["chars"] for m in manifest)
    print(f"jobs: {len(manifest)}  works: {len({m['slug'] for m in manifest})}  "
          f"total_chars: {total_chars:,}")


if __name__ == "__main__":
    main()
