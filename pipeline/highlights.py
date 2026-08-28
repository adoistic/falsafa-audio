#!/usr/bin/env python3
"""
Curated song highlights — the "creative song" layer.

Full-corpus song generation is off the table (every open song model runs 35-51% lyric
WER; our narration runs 3.2%). Instead: from every poetic and mixed work, pick a handful
of genuinely impactful passages and render only those as songs. Small enough that each
one can be worth listening to, wide enough that every work in the corpus has one.

Stages:
  candidates  extract stanza-sized windows from each work's song units (local, cheap)
  batches     group works into subagent-sized batches
  merge       fold subagent picks back into one selections.json, validating every locator

  python3 pipeline/highlights.py candidates
  python3 pipeline/highlights.py batches --per-batch 8
  python3 pipeline/highlights.py merge
"""
from __future__ import annotations
import argparse, json, os, re, sys, unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DATASET = os.path.join(REPO, "dataset")
CLASSIFICATION = os.path.join(REPO, "classification")
HL = os.path.join(REPO, "highlights")

# a candidate window: stanza-sized, singable, not a fragment. Line count is NOT a
# criterion — a lot of the verse embedded in prose works arrives as one wrapped
# paragraph, and those quotations are exactly the passages worth singing.
MAX_LINES = 16
MIN_CHARS, MAX_CHARS = 200, 780
FLOOR_CHARS = 90          # a work with only a scrap of verse still gets its one shot
CANDIDATES_PER_WORK = 24

# lines that are structural noise rather than verse
_NOISE = re.compile(r"^\s*(\*\*[^*]+\*\*|\[[^\]]*\]|-->|\(\d+\)|[IVXLC]+\.?|\d+\.?)\s*$")
_MARKUP = re.compile(r"\*\*[^*]+\*\*|-->|^\s*\[[^\]]*\]\s*")


def clean_line(ln):
    ln = _MARKUP.sub("", ln).strip()
    return ln


def song_lines(slug):
    """All verse lines of a work, in order, with their source unit index."""
    out = []
    path = os.path.join(DATASET, f"{slug}.jsonl")
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8"):
        d = json.loads(line)
        if d["mode"] != "song":
            continue
        for i, raw in enumerate(d["text"].split("\n")):
            if _NOISE.match(raw):
                continue
            ln = clean_line(raw)
            if ln:
                out.append((d["unit"], i, ln))
    return out


def windows(lines):
    """Non-overlapping stanza-sized windows covering the work."""
    out, i, rejected = [], 0, []
    while i < len(lines):
        chunk, chars = [], 0
        while i < len(lines) and len(chunk) < MAX_LINES and chars < MAX_CHARS:
            chars += len(lines[i][2]) + 1
            chunk.append(lines[i])
            i += 1
        (out if chars >= MIN_CHARS else rejected).append(chunk)
    if not out:                      # fall back to the longest scrap the work has
        best = max(rejected, key=lambda c: sum(len(l[2]) for l in c), default=None)
        if best and sum(len(l[2]) for l in best) >= FLOOR_CHARS:
            out = [best]
    return out


def spread(items, n):
    """Evenly sample n items across the whole sequence (not just the opening)."""
    if len(items) <= n:
        return list(range(len(items)))
    step = len(items) / n
    return [int(i * step) for i in range(n)]


def cmd_candidates(args):
    works = {w["slug"]: w for w in json.load(open(f"{CLASSIFICATION}/works.json"))}
    os.makedirs(HL, exist_ok=True)
    out, skipped = [], []
    for slug, w in sorted(works.items()):
        if w.get("class") not in ("poetic", "mixed"):
            continue
        wins = windows(song_lines(slug))
        if not wins:
            skipped.append(slug)
            continue
        idx = spread(wins, CANDIDATES_PER_WORK)
        cands = []
        for c, wi in enumerate(idx):
            chunk = wins[wi]
            cands.append({
                "id": f"c{c:02d}",
                "at": f"{chunk[0][0]}:{chunk[0][1]}",       # unit:line locator
                "text": "\n".join(ln for _, _, ln in chunk),
            })
        out.append({"slug": slug, "title": w["title"], "author": w["author"],
                    "language": w["language"], "genre": w.get("genre", ""),
                    "era": w.get("era", ""), "class": w["class"],
                    "n_windows": len(wins), "candidates": cands})
    json.dump(out, open(f"{HL}/candidates.json", "w"), ensure_ascii=False)
    tot = sum(len(w["candidates"]) for w in out)
    print(f"{len(out)} works, {tot} candidate passages "
          f"({tot*sum(len(c['text']) for w in out for c in w['candidates'])//max(tot,1)/1e6:.1f}M chars)")
    if skipped:
        print(f"no usable windows: {len(skipped)} works -> {skipped[:6]}")


def cmd_batches(args):
    cands = json.load(open(f"{HL}/candidates.json"))
    bdir = os.path.join(HL, "batches")
    os.makedirs(bdir, exist_ok=True)
    for f in os.listdir(bdir):
        os.remove(os.path.join(bdir, f))
    # pack by character weight so no batch is a monster
    cands.sort(key=lambda w: -sum(len(c["text"]) for c in w["candidates"]))
    batches, n = [[] for _ in range(max(1, len(cands) // args.per_batch))], 0
    for w in cands:                                    # round-robin = even weight
        batches[n % len(batches)].append(w)
        n += 1
    for i, b in enumerate(batches):
        json.dump(b, open(f"{bdir}/batch_{i:03d}.json", "w"), ensure_ascii=False, indent=1)
    print(f"{len(batches)} batches in {bdir} "
          f"({min(len(b) for b in batches)}-{max(len(b) for b in batches)} works each)")


_GLOSS = re.compile(r"\{\{[^:}]*:([^}]*)\}\}")      # {{bhairava:the terrible Absolute}}
_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_CITE = re.compile(r"^\s*\(?\d+[-–]\d+\)?\s*$", re.M)


def clean_lyrics(s):
    """Editorial apparatus that reads fine on a page and terribly when sung."""
    s = _GLOSS.sub(r"\1", s)
    s = _COMMENT.sub("", s)
    s = _CITE.sub("", s)
    s = _MARKUP.sub("", s)
    # ACE-Step romanizes diacritics stochastically; fold them ourselves so the lyrics it
    # is given are at least deterministic. Ṛgveda -> Rgveda, Śiva -> Siva.
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return "\n".join(ln.rstrip() for ln in s.splitlines() if ln.strip())


LYRIC_CAP = 1200        # ~3-4 minutes sung; the model's own cap is 4096 but nobody
THIN = 150              # wants a six-minute song out of one prose paragraph


def trim(s):
    """Cut at a line boundary, not mid-word. Windows are char-bounded but a single
    wrapped-prose 'line' can be thousands of characters on its own."""
    if len(s) <= LYRIC_CAP:
        return s, False
    out = []
    for ln in s.split("\n"):
        if sum(len(x) + 1 for x in out) + len(ln) > LYRIC_CAP and out:
            break
        out.append(ln)
    return ("\n".join(out) or s[:LYRIC_CAP].rsplit(" ", 1)[0]), True


def cmd_merge(args):
    cands = {w["slug"]: w for w in json.load(open(f"{HL}/candidates.json"))}
    rdir = os.path.join(HL, "results")
    picks, bad, files = [], 0, sorted(os.listdir(rdir)) if os.path.isdir(rdir) else []
    for f in files:
        if not f.endswith(".json"):
            continue
        try:
            data = json.load(open(os.path.join(rdir, f)))
        except Exception as e:
            print(f"  UNREADABLE {f}: {e}")
            continue
        for sel in data:
            w = cands.get(sel.get("slug"))
            if not w:
                bad += 1; continue
            by_id = {c["id"]: c for c in w["candidates"]}
            for p in sel.get("picks", []):
                c = by_id.get(p.get("id"))
                if not c:
                    bad += 1; continue
                lyrics, cut = trim(clean_lyrics(c["text"]))
                picks.append({
                    "slug": w["slug"], "title": w["title"], "author": w["author"],
                    "language": w["language"], "era": w["era"], "class": w["class"],
                    "cand": c["id"], "at": c["at"], "lyrics": lyrics,
                    "style": p.get("style", ""), "why": p.get("why", ""),
                    **({"trimmed": True} if cut else {}),
                    **({"thin": True} if len(lyrics) < THIN else {}),
                })
    json.dump(picks, open(f"{HL}/selections.json", "w"), ensure_ascii=False, indent=1)
    covered = len({p["slug"] for p in picks})
    print(f"{len(picks)} selections across {covered}/{len(cands)} works "
          f"({len(files)} result files, {bad} invalid refs dropped, "
          f"{sum('trimmed' in p for p in picks)} trimmed to {LYRIC_CAP} chars, "
          f"{sum('thin' in p for p in picks)} flagged thin)")
    missing = sorted(set(cands) - {p["slug"] for p in picks})
    if missing:
        print(f"uncovered works: {len(missing)} -> {missing[:8]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("candidates")
    b = sub.add_parser("batches"); b.add_argument("--per-batch", type=int, default=8)
    sub.add_parser("merge")
    a = ap.parse_args()
    {"candidates": cmd_candidates, "batches": cmd_batches, "merge": cmd_merge}[a.cmd](a)
