#!/usr/bin/env python3
"""
Embedded-verse candidate detector (pre-filter for the Phase 3 LLM pass).

Scans every prose chapter of every non-pure-poetry work for textual cues that verse is quoted
inside the prose (epigrams, epitaphs, hymns, "wrote thus:", "the following lines", …). Chapters
with >= CUE_THRESHOLD cues become LLM candidates. Pure-poetry works (class == "poetic") are
skipped entirely — their verse is already all-song and needs no internal split.

Output: classification/candidates.json
  [ {slug, chapter_slug, variant, n_paragraphs, cue_hits} , ... ]
"""
from __future__ import annotations
import json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CORPUS = "/Users/siraj/falsafa/corpus"
CUE_THRESHOLD = 2

CUE = re.compile(
    r"\b(verses?|epigram|epitaph|these lines|the following lines|the following verses|"
    r"runs thus|wrote thus|writes thus|sang thus|as follows|in these words|thus:|poem|"
    r"couplet|distich|stanza|\bode\b|hymn|elegy|sings|sang|to quote|quoted)\b",
    re.I,
)

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
    works = {w["slug"]: w for w in json.load(open(os.path.join(REPO, "classification/works.json")))}
    candidates = []
    scanned = 0
    for slug, w in works.items():
        if w.get("class") == "poetic":
            continue  # skip pure poetry (incl. long pure poetry)
        chdir_root = os.path.join(CORPUS, "works", slug, "chapters")
        if not os.path.isdir(chdir_root):
            continue
        for ch in sorted(os.listdir(chdir_root)):
            chdir = os.path.join(chdir_root, ch)
            mpath = os.path.join(chdir, "meta.json")
            if not os.path.exists(mpath):
                continue
            m = json.load(open(mpath))
            if m.get("layout") != "prose":
                continue
            scanned += 1
            variant = m.get("default_variant") or "translation.md"
            md = os.path.join(chdir, variant)
            if not os.path.exists(md):
                mds = [f for f in os.listdir(chdir) if f.endswith(".md")]
                if not mds:
                    continue
                variant = mds[0]
                md = os.path.join(chdir, variant)
            body = _read_frontmatter_and_body(md)
            hits = len(CUE.findall(body))
            if hits >= CUE_THRESHOLD:
                paras = paragraphs_for(chdir, variant)
                candidates.append({
                    "slug": slug,
                    "chapter_slug": m.get("chapter_slug", ch),
                    "chapter_dir": ch,
                    "variant": variant,
                    "n_paragraphs": len(paras),
                    "cue_hits": hits,
                })
    candidates.sort(key=lambda x: (x["slug"], x["chapter_dir"]))
    out = os.path.join(REPO, "classification/candidates.json")
    json.dump(candidates, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    n_works = len({c["slug"] for c in candidates})
    print(f"prose chapters scanned: {scanned}")
    print(f"candidate chapters: {len(candidates)}  across {n_works} works")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
