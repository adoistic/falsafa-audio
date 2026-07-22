#!/usr/bin/env python3
"""
Falsafa-audio segmenter (reconstructed).

Turns a corpus work into audio units:
  - Each chapter has a mode from meta.json.layout: verse -> "song", prose -> "narration".
  - Paragraphs (from <variant>.paragraphs.json) are flattened across chapters in order.
  - Greedy chunking: accumulate consecutive same-mode paragraphs; emit a unit once the
    running character total crosses the budget (song 1,800 / narration 3,500), or when the
    mode changes. Units may therefore span consecutive same-mode chapters. A single
    oversized paragraph becomes its own (oversized) unit.
  - ref_start/ref_end = "<chapter_slug>:<paragraph_offset>".

Phase 3+ can pass `verse_spans` per chapter to override mode at sub-chapter granularity
(embedded verse inside a prose chapter -> song). See segment_work(overrides=...).

Usage:
  from segment import segment_work, load_chapters
  units = segment_work(slug, corpus_root)
"""
from __future__ import annotations
import json, os, re
from dataclasses import dataclass

SONG_BUDGET = 1800
NARR_BUDGET = 3500

@dataclass
class Para:
    chapter_slug: str
    offset: int
    text: str
    mode: str  # "song" | "narration"

def _read_frontmatter_and_body(md_path: str):
    t = open(md_path, encoding="utf-8").read()
    if t.startswith("---"):
        parts = t.split("---", 2)
        if len(parts) == 3:
            return parts[2].lstrip("\n")
    return t

def _chapter_dirs(work_dir: str):
    chdir = os.path.join(work_dir, "chapters")
    if not os.path.isdir(chdir):
        return []
    def sort_key(name):
        m = re.match(r"(\d+)", name)
        return (int(m.group(1)) if m else 10**9, name)
    return [os.path.join(chdir, d) for d in sorted(os.listdir(chdir), key=sort_key)
            if os.path.isdir(os.path.join(chdir, d))]

def load_work_meta(slug: str, corpus_root: str) -> dict:
    """Read index.md frontmatter for title/language/genre."""
    idx = os.path.join(corpus_root, "works", slug, "index.md")
    meta = {"slug": slug, "title": slug, "language": "English", "genre": "Unknown"}
    if not os.path.exists(idx):
        return meta
    t = open(idx, encoding="utf-8").read()
    if t.startswith("---"):
        fm = t.split("---", 2)[1]
        for key in ("title", "genre", "language"):
            m = re.search(rf"^{key}:\s*(.+)$", fm, re.M)
            if m:
                meta[key] = m.group(1).strip().strip('"')
    return meta

def load_chapters(slug: str, corpus_root: str):
    """Return list of dicts: {chapter_slug, layout, variant_file, paragraphs:[{offset,text}]}."""
    work_dir = os.path.join(corpus_root, "works", slug)
    out = []
    for chdir in _chapter_dirs(work_dir):
        mpath = os.path.join(chdir, "meta.json")
        if not os.path.exists(mpath):
            continue
        m = json.load(open(mpath))
        layout = m.get("layout", "prose")
        variant = m.get("default_variant") or "translation.md"
        # paragraphs file matches the variant base name
        pbase = variant.rsplit(".md", 1)[0]
        ppath = os.path.join(chdir, f"{pbase}.paragraphs.json")
        if not os.path.exists(ppath):
            # fall back to any paragraphs file present
            cands = [f for f in os.listdir(chdir) if f.endswith(".paragraphs.json")]
            # prefer translation, then original
            cands.sort(key=lambda f: (0 if f.startswith("translation") else 1, f))
            ppath = os.path.join(chdir, cands[0]) if cands else None
        paragraphs = []
        if ppath and os.path.exists(ppath):
            paragraphs = json.load(open(ppath))
        else:
            # no paragraphs.json → derive from the variant markdown body
            mdpath = os.path.join(chdir, variant)
            if not os.path.exists(mdpath):
                mds = [f for f in os.listdir(chdir) if f.endswith(".md")]
                mdpath = os.path.join(chdir, mds[0]) if mds else None
            if mdpath and os.path.exists(mdpath):
                paragraphs = _paragraphs_from_body(_read_frontmatter_and_body(mdpath))
        out.append({
            "chapter_slug": m.get("chapter_slug", os.path.basename(chdir)),
            "layout": layout,
            "variant_file": variant,
            "paragraphs": paragraphs,
        })
    return out

def _paragraphs_from_body(body: str):
    """Fallback paragraph index when <variant>.paragraphs.json is absent (e.g. ECPA verse
    works that ship only original.md). Blocks are separated by blank lines; offset is the
    char index of the block start within the frontmatter-stripped body — same convention as
    the corpus paragraphs.json files."""
    out = []
    for m in re.finditer(r"\S.*?(?=\n[ \t]*\n|\Z)", body, re.S):
        text = m.group().rstrip()
        if text.strip():
            out.append({"offset": m.start(), "text": text})
    return out

def _mode_for(layout: str) -> str:
    return "song" if layout == "verse" else "narration"

_MARKER = re.compile(r"^[\W\d_]{0,14}$")  # only digits/punctuation/markup, <=14 chars

def _is_marker(text: str) -> bool:
    """A trivial structural marker (bare stanza/section number like '**68**', '1.', '—').
    Has no letters, so it can never be real verse or real prose content."""
    return bool(_MARKER.fullmatch(text.strip()))

def _flatten(chapters, overrides=None):
    """Flatten chapters into a Para stream. `overrides` maps chapter_slug -> set/list of
    paragraph OFFSETS that are embedded VERSE inside an otherwise-prose chapter; those
    paragraphs become mode 'song'. Trivial marker paragraphs inherit the running mode so a
    bare stanza number between verse stanzas doesn't fragment the song into tiny units."""
    overrides = overrides or {}
    stream = []
    prev_mode = None
    for ch in chapters:
        base_mode = _mode_for(ch["layout"])
        verse_offsets = overrides.get(ch["chapter_slug"])
        for p in ch["paragraphs"]:
            o = p["offset"]
            if _is_marker(p["text"]) and prev_mode is not None:
                mode = prev_mode
            else:
                mode = "song" if (verse_offsets and o in verse_offsets) else base_mode
            stream.append(Para(ch["chapter_slug"], o, p["text"], mode))
            prev_mode = mode
    return stream

def _chunk(stream):
    """Greedy: accumulate same-mode paras; emit once running total crosses budget."""
    units = []
    cur = []
    cur_mode = None
    total = 0
    def budget(mode):
        return SONG_BUDGET if mode == "song" else NARR_BUDGET
    def emit():
        nonlocal cur, total
        if not cur:
            return
        text = "\n".join(p.text for p in cur)
        units.append({
            "mode": cur_mode,
            "ref_start": f"{cur[0].chapter_slug}:{cur[0].offset}",
            "ref_end": f"{cur[-1].chapter_slug}:{cur[-1].offset}",
            "chars": len(text),
            "text": text,
        })
        cur = []
        total = 0
    for p in stream:
        if cur and p.mode != cur_mode:
            emit()
        cur_mode = p.mode
        # running total mirrors len("\n".join(texts)) so the budget boundary matches
        total += len(p.text) + (1 if cur else 0)
        cur.append(p)
        if total >= budget(cur_mode):
            emit()
    emit()
    return units

def segment_work(slug: str, corpus_root: str, overrides=None):
    meta = load_work_meta(slug, corpus_root)
    chapters = load_chapters(slug, corpus_root)
    stream = _flatten(chapters, overrides)
    raw = _chunk(stream)
    units = []
    for i, u in enumerate(raw):
        if not u["text"].strip():
            continue
        units.append({
            "work": slug,
            "title": meta["title"],
            "language": meta["language"],
            "mode": u["mode"],
            "unit": i,
            "ref_start": u["ref_start"],
            "ref_end": u["ref_end"],
            "chars": u["chars"],
            "text": u["text"],
        })
    # renumber after any skips
    for i, u in enumerate(units):
        u["unit"] = i
    return units, meta, chapters

if __name__ == "__main__":
    import sys
    corpus = sys.argv[2] if len(sys.argv) > 2 else "/Users/siraj/falsafa/corpus"
    units, meta, chapters = segment_work(sys.argv[1], corpus)
    song = sum(1 for u in units if u["mode"] == "song")
    narr = sum(1 for u in units if u["mode"] == "narration")
    print(f"{sys.argv[1]}: {len(units)} units (song={song} narration={narr})")
    for u in units[:3]:
        print(f"  unit {u['unit']} {u['mode']} chars={u['chars']} {u['ref_start']} -> {u['ref_end']}")
