#!/usr/bin/env python3
"""
Merge per-job subagent results (classification/verse-spans/<jobid>.json) into the single
classification/verse-spans.json consumed by build.py, validating every returned offset against
the job's real paragraph offsets (drops hallucinated / out-of-range offsets).

verse-spans/<jobid>.json produced by a subagent = {"verse_offsets": [int, ...]}
Output verse-spans.json = { "<slug>": { "<chapter_slug>": [sorted unique valid offsets] } }

Also prints coverage: jobs done / total, works with verse, total verse paragraphs.
"""
from __future__ import annotations
import json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
JOBS = os.path.join(REPO, "classification/verse-jobs")
RESULTS = os.path.join(REPO, "classification/verse-spans")


def valid_offsets_for(jobid):
    """Parse the job's txt to get the set of legitimate paragraph offsets."""
    path = os.path.join(JOBS, f"{jobid}.txt")
    if not os.path.exists(path):
        return None
    offs = set()
    for m in re.finditer(r"^\[(\d+)\] ", open(path, encoding="utf-8").read(), re.M):
        offs.add(int(m.group(1)))
    return offs


def main():
    manifest = json.load(open(os.path.join(JOBS, "manifest.json")))
    by_job = {m["jobid"]: m for m in manifest}
    spans = {}
    done = 0
    dropped = 0
    verse_total = 0
    for jobid, m in by_job.items():
        rp = os.path.join(RESULTS, f"{jobid}.json")
        if not os.path.exists(rp):
            continue
        done += 1
        try:
            res = json.load(open(rp))
        except Exception:
            continue
        offs = res.get("verse_offsets", []) if isinstance(res, dict) else []
        valid = valid_offsets_for(jobid) or set()
        keep = sorted({o for o in offs if o in valid})
        dropped += len([o for o in offs if o not in valid])
        if keep:
            spans.setdefault(m["slug"], {}).setdefault(m["chapter_slug"], [])
            spans[m["slug"]][m["chapter_slug"]].extend(keep)
            verse_total += len(keep)
    # dedupe + sort per chapter
    for slug in spans:
        for ch in spans[slug]:
            spans[slug][ch] = sorted(set(spans[slug][ch]))
    out = os.path.join(REPO, "classification/verse-spans.json")
    json.dump(spans, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"jobs done: {done}/{len(by_job)}  ({100*done//max(len(by_job),1)}%)")
    print(f"works with embedded verse: {len(spans)}  |  verse paragraphs: {verse_total}  "
          f"|  invalid offsets dropped: {dropped}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
