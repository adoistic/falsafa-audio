#!/usr/bin/env python3
"""
Group Phase-3 jobs into size-bounded batches for subagent dispatch, and report which batches
are not yet complete (all their job results present).

A batch closes at BATCH_MAX_JOBS jobs or BATCH_MAX_CHARS characters, whichever first.
Writes classification/verse-batches/batch-NNN.json = {"batch": N, "jobids": [...]}.

Usage:
  python3 pipeline/plan_batches.py            # (re)write batches + print pending batches
  python3 pipeline/plan_batches.py --pending  # just list batch ids with unfinished jobs
"""
from __future__ import annotations
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
JOBS = os.path.join(REPO, "classification/verse-jobs")
RESULTS = os.path.join(REPO, "classification/verse-spans")
BATCHES = os.path.join(REPO, "classification/verse-batches")
BATCH_MAX_JOBS = 20
BATCH_MAX_CHARS = 700_000


def build_batches():
    manifest = json.load(open(os.path.join(JOBS, "manifest.json")))
    # keep a stable order; group so a single work's chunks tend to land together
    manifest.sort(key=lambda m: (m["slug"], m["chapter_slug"], m["part"]))
    batches = []
    cur, cur_chars = [], 0
    for m in manifest:
        if cur and (len(cur) >= BATCH_MAX_JOBS or cur_chars + m["chars"] > BATCH_MAX_CHARS):
            batches.append(cur)
            cur, cur_chars = [], 0
        cur.append(m["jobid"])
        cur_chars += m["chars"]
    if cur:
        batches.append(cur)
    return batches


def main():
    os.makedirs(BATCHES, exist_ok=True)
    batches = build_batches()
    if "--pending" not in sys.argv:
        for i, jobids in enumerate(batches):
            json.dump({"batch": i, "jobids": jobids},
                      open(os.path.join(BATCHES, f"batch-{i:03d}.json"), "w"))
    # report pending
    pending = []
    done_jobs = {fn[:-5] for fn in os.listdir(RESULTS)} if os.path.isdir(RESULTS) else set()
    for i, jobids in enumerate(batches):
        if not all(j in done_jobs for j in jobids):
            remaining = [j for j in jobids if j not in done_jobs]
            pending.append((i, len(remaining), len(jobids)))
    print(f"total batches: {len(batches)}  |  pending: {len(pending)}  |  "
          f"job results present: {len(done_jobs)}")
    if "--pending" in sys.argv:
        for i, rem, tot in pending:
            print(f"  batch-{i:03d}: {rem}/{tot} jobs remaining")


if __name__ == "__main__":
    main()
