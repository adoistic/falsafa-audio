#!/usr/bin/env python3
"""
Merge raw stream alignments into the sidecars the reader UI consumes.

Inputs
  dataset/<work>.jsonl                       units (mode, unit, ref_start/end, text)
  corpus works/<work>/chapters/*/            meta.json + <variant>.paragraphs.json
  align/_streams/<prefix>__<slug>.json.gz    raw word rows [local_unit, s_ms, e_ms, score]
  classification/works.json                  work class (prose/mixed/poetic)

Outputs (per work, uploaded to r2:falsafa-audio/align/)
  align/<work>/book.json                     streams + per-chapter spans (listen mode)
  align/<work>/ch/<chapter>.json             per-paragraph blocks with line/word times
                                             (read-along mode; lines carry their text)
Site build data (written into the falsafa repo)
  apps/site/src/data/audio-manifest.json     which works/chapters have audio + durations
  apps/site/src/data/verse-blocks.json       embedded-verse paragraph ids per prose
                                             chapter (read-mode styling; derived from
                                             dataset alone, no alignment needed)

Data contract (all times ms from the START of the referenced stream):
  book.json  {"v":1,"slug","streams":[{"id","url","dur"}],
              "chapters":[{"ch","title","dur","spans":[[stream_idx,s,e],...]}]}
  ch/*.json  {"v":1,"work","ch","streams":[...used subset, same shape],
              "blocks":[{"p":"p-xxx","kind":"verse"|"prose","stream":idx,"s","e",
                         "lines":[{"s","e","t","w":[[s,e,off,len],...]}]}]}
  Word rows are [start_ms,end_ms,char_off,char_len] with offsets into the LINE text.

A "line" is never a whole paragraph: verse lines are the \n-split poetic lines;
prose paragraphs are split into sentences (bounded at ~220 chars as a fallback).

Usage:
  python3 align_merge.py --emit-verse-blocks          # no alignment needed
  python3 align_merge.py --work <slug>                # one work -> out/align/
  python3 align_merge.py --all [--upload]             # everything + R2 upload
"""
from __future__ import annotations
import argparse, gzip, json, os, re, subprocess, sys, time
from collections import defaultdict

from align_common import TOKENIZER_V, tokenize

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DATASET = os.path.join(REPO, "dataset")
OUT = os.path.join(REPO, "out", "align")
CORPUS_ROOT = "/Users/siraj/falsafa/corpus"
SITE_DATA = "/Users/siraj/falsafa/apps/site/src/data"
STREAMS_DIR = os.path.join(REPO, "out", "_streams")
BUCKET = "r2:falsafa-audio"

CHAPTER_PAD_MS = 300     # breath after a chapter's last word

log = lambda *a: (print(*a), sys.stdout.flush())


# ── corpus paragraph loading (mirrors segment.py's variant selection) ──────

def _chapter_dirs(work_dir):
    chdir = os.path.join(work_dir, "chapters")
    if not os.path.isdir(chdir):
        return []
    def sort_key(name):
        m = re.match(r"(\d+)", name)
        return (int(m.group(1)) if m else 10**9, name)
    return [os.path.join(chdir, d) for d in sorted(os.listdir(chdir), key=sort_key)
            if os.path.isdir(os.path.join(chdir, d))]


def _md_body(path):
    t = open(path, encoding="utf-8").read()
    if t.startswith("---"):
        parts = t.split("---", 2)
        if len(parts) == 3:
            return parts[2].lstrip("\n")
    return t


def _paras_from_body(body):
    """Fallback paragraph index when no .paragraphs.json exists (ECPA verse
    works shipping only original.md) — same convention as segment.py."""
    out = []
    for m in re.finditer(r"\S.*?(?=\n[ \t]*\n|\Z)", body, re.S):
        text = m.group().rstrip()
        if text.strip():
            out.append({"offset": m.start(), "text": text})
    return out


def load_chapter_paras(slug):
    """[{ch, title, layout, paras:[{offset,text,id}]}] in reading order."""
    out = []
    for chdir in _chapter_dirs(os.path.join(CORPUS_ROOT, "works", slug)):
        mpath = os.path.join(chdir, "meta.json")
        if not os.path.exists(mpath):
            continue
        m = json.load(open(mpath))
        variant = m.get("default_variant") or "translation.md"
        pbase = variant.rsplit(".md", 1)[0]
        ppath = os.path.join(chdir, f"{pbase}.paragraphs.json")
        if not os.path.exists(ppath):
            cands = [f for f in os.listdir(chdir) if f.endswith(".paragraphs.json")]
            cands.sort(key=lambda f: (0 if f.startswith("translation") else 1, f))
            ppath = os.path.join(chdir, cands[0]) if cands else None
        paras = json.load(open(ppath)) if ppath and os.path.exists(ppath) else []
        if not paras:
            # derive from the variant markdown, mirroring segment.py, so the
            # ref offsets in the dataset line up (paras carry no DOM ids)
            mdpath = os.path.join(chdir, variant)
            if not os.path.exists(mdpath):
                mds = [f for f in os.listdir(chdir) if f.endswith(".md")]
                mdpath = os.path.join(chdir, mds[0]) if mds else None
            if mdpath and os.path.exists(mdpath):
                paras = _paras_from_body(_md_body(mdpath))
        out.append({"ch": m.get("chapter_slug", os.path.basename(chdir)),
                    "title": m.get("chapter_title", ""),
                    "layout": m.get("layout", "prose"),
                    "paras": paras})
    return out


def build_para_stream(chapters):
    """Flattened [(ch, key, text, id, layout)] + two indexes:
      by (ch, offset) for the classic '<chapter>:<offset>' refs, and
      by verse ref string for the GRETIL-epic sidecars whose paragraphs
      carry {'ref': '1.1.1', 'id': ..., 'text': ...} instead of offsets."""
    stream, index, ref_index = [], {}, {}
    for c in chapters:
        for p in c["paras"]:
            if not isinstance(p, dict) or "text" not in p:
                continue
            if "offset" in p:
                index[(c["ch"], p["offset"])] = len(stream)
                stream.append((c["ch"], p["offset"], p["text"], p.get("id"), c["layout"]))
            elif "ref" in p:
                ref_index.setdefault(str(p["ref"]), len(stream))
                stream.append((c["ch"], p["ref"], p["text"], p.get("id"), c["layout"]))
    return stream, index, ref_index


def unit_paragraphs(unit, stream, index, ref_index):
    """Which flattened paragraphs make up this unit, with unit-relative offsets.
    Returns [(stream_idx, unit_off)] or None if reconstruction fails.

    Two ref grammars: classic '<chapter_slug>:<char_offset>' (paragraph
    sidecars with offsets, strict '\n' join) and GRETIL-epic 'b.c.v' verse
    refs (sidecars keyed by ref; separators vary, so match flexibly)."""
    text = unit["text"]

    def classic(r):
        ch, off = r.rsplit(":", 1)
        return ch, int(off)

    try:
        i0 = index[classic(unit["ref_start"])]
        i1 = index[classic(unit["ref_end"])]
        out, pos = [], 0
        for i in range(i0, i1 + 1):
            ptext = stream[i][2]
            if not text.startswith(ptext, pos):
                return None
            out.append((i, pos))
            pos += len(ptext) + 1          # "\n" join
        if pos - 1 != len(text):
            return None
        return out
    except (KeyError, ValueError):
        pass

    i0 = ref_index.get(str(unit["ref_start"]))
    i1 = ref_index.get(str(unit["ref_end"]))
    if i0 is None or i1 is None or i1 < i0:
        return None
    out, pos = [], 0
    for i in range(i0, i1 + 1):
        ptext = stream[i][2]
        while pos < len(text) and text[pos] in " \t\n":
            pos += 1
        if not text.startswith(ptext, pos):
            return None
        out.append((i, pos))
        pos += len(ptext)
    if text[pos:].strip():                  # only trailing whitespace may remain
        return None
    return out


# ── line splitting ─────────────────────────────────────────────────────────

_ABBREV = re.compile(
    r"\b(Mr|Mrs|Ms|Dr|St|Prof|Rev|Hon|Gen|Col|Capt|Lt|Sgt|vs|etc|viz|cf|al|"
    r"e\.g|i\.e|No|Vol|Ch|pp|p|fig|Fig)\.$"
)
_SENT_END = re.compile(r'[.!?]["\'“”‘’\)\]]*\s+')
MAX_LINE = 220


def split_sentences(text):
    """[(start, end)] sentence-ish ranges over prose text. Never the whole
    paragraph unless it is genuinely one sentence; long runs fall back to
    comma/space breaks near MAX_LINE."""
    spans, start = [], 0
    for m in _SENT_END.finditer(text):
        end = m.start() + 1
        # keep going past abbreviations and single-initial breaks ("W. Scott")
        prefix = text[start:end]
        if _ABBREV.search(prefix) or re.search(r"\b[A-Z]\.$", prefix):
            continue
        spans.append((start, end))
        start = m.end()
    if start < len(text):
        spans.append((start, len(text)))
    # bound runaway sentences
    out = []
    for s, e in spans:
        while e - s > MAX_LINE:
            cut = text.rfind(", ", s + MAX_LINE // 2, s + MAX_LINE)
            if cut == -1:
                cut = text.rfind(" ", s + MAX_LINE // 2, s + MAX_LINE)
            if cut == -1:
                break
            out.append((s, cut + 1))
            s = cut + 2 if text[cut] == "," else cut + 1
        out.append((s, e))
    return [(s, e) for s, e in out if text[s:e].strip()]


def split_lines(ptext, kind):
    """Paragraph text -> [(start, end)] line ranges (verse: \n lines)."""
    if kind == "verse":
        spans, pos = [], 0
        for ln in ptext.split("\n"):
            if ln.strip():
                spans.append((pos, pos + len(ln)))
            pos += len(ln) + 1
        return spans
    return split_sentences(ptext)


# ── stream routing ─────────────────────────────────────────────────────────

def epic_book(unit):
    """Mahabharata: parva number from ref_start's leading chapter digits."""
    m = re.match(r"(\d+)", unit["ref_start"])
    return int(m.group(1)) if m else None


try:
    from split_epics import PARVAS
except ImportError:
    PARVAS = {}

# Spoken-text token maps for the regenerated Indic works: the TTS/alignment
# ran on a normalized SPOKEN variant (see tts_normalize.py); each aligned row
# maps back to a display-token index. Keys "<track>:<stream_slug>".
_TTS_MAPS = None


def tts_maps():
    global _TTS_MAPS
    if _TTS_MAPS is None:
        p = os.path.join(REPO, "out", "tts_maps.json.gz")
        _TTS_MAPS = json.load(gzip.open(p, "rt")) if os.path.exists(p) else {}
    return _TTS_MAPS


def work_streams(slug, wclass, units):
    """[(stream_id, url_slug_path, [global_unit_idx,...])] for this work.
    stream_id keys raw alignment files; url path keys the HLS folder."""
    narr = [u["unit"] for u in units if u["mode"] == "narration"]
    song = [u["unit"] for u in units if u["mode"] == "song"]
    out = []
    if slug == "mahabharata":
        by_book = defaultdict(list)
        for u in units:
            if u["mode"] != "song":
                continue
            b = epic_book(u)
            if b is not None:
                by_book[b].append(u["unit"])
        for b in sorted(by_book):
            name = PARVAS.get(b)
            if not name:
                continue
            pslug = f"mahabharata-book-{b:02d}-{name.lower()}"   # matches split_epics slugs
            out.append((f"verse:{pslug}", f"verse/{pslug}", by_book[b]))
        return out
    if narr:
        out.append(("narration", f"works/{slug}", narr))
    if song:
        out.append((f"verse", f"verse/{slug}", song))
    return out


def raw_alignment_path(url_path):
    prefix, slug = url_path.split("/", 1)
    return os.path.join(STREAMS_DIR, f"{prefix}__{slug}.json.gz")


# ── merge one work ─────────────────────────────────────────────────────────

def merge_work(slug, wclass, verbose=True):
    ds_path = os.path.join(DATASET, f"{slug}.jsonl")
    if not os.path.exists(ds_path):
        return None, "no-dataset"
    units = [json.loads(l) for l in open(ds_path, encoding="utf-8")]
    if not units:
        return None, "empty-dataset"

    chapters = load_chapter_paras(slug)
    if not chapters:
        return None, "no-chapters"
    stream, index, ref_index = build_para_stream(chapters)
    ch_meta = {c["ch"]: c for c in chapters}
    ch_order = [c["ch"] for c in chapters]

    routes = work_streams(slug, wclass, units)
    if not routes:
        return None, "no-streams"
    unit_by_id = {u["unit"]: u for u in units}

    # load raw alignments; every stream must exist for full merge
    streams_out, word_rows = [], {}     # stream_idx -> per-unit word rows
    for sid, url_path, uids in routes:
        p = raw_alignment_path(url_path)
        if not os.path.exists(p):
            return None, f"missing-alignment:{url_path}"
        raw = json.load(gzip.open(p, "rt"))
        if raw.get("tokenizer") != TOKENIZER_V:
            return None, f"tokenizer-mismatch:{url_path}"
        rows_by_local = defaultdict(list)
        for r in raw["words"]:
            rows_by_local[r[0]].append(r)
        # local unit i == i-th uid in this stream's route order
        per_unit = {}
        for li, gid in enumerate(uids):
            per_unit[gid] = rows_by_local.get(li, [])
        word_rows[len(streams_out)] = per_unit
        streams_out.append({"id": sid, "url": f"/audio/{url_path}/index.m3u8",
                            "dur": raw["duration_ms"]})

    # per-chapter accumulation
    blocks_by_ch = defaultdict(list)
    fail_units = 0
    for si, (sid, url_path, uids) in enumerate(routes):
        mkey = sid if ":" in sid else f"{sid}:{slug}"
        stream_maps = tts_maps().get(mkey)
        for li_u, gid in enumerate(uids):
            u = unit_by_id[gid]
            paras = unit_paragraphs(u, stream, index, ref_index)
            rows = word_rows[si].get(gid, [])
            if paras is None:
                fail_units += 1
                continue
            if not rows:
                continue
            toks = tokenize(u["text"])
            if stream_maps is not None:
                # aligned rows are SPOKEN tokens; map back to display tokens
                map_u = stream_maps[li_u] if li_u < len(stream_maps) else None
                if map_u is None or len(rows) != len(map_u):
                    fail_units += 1
                    continue
                pairs = [(toks[di][1], r) for r, di in zip(rows, map_u)
                         if di < len(toks)]
            else:
                if len(toks) != len(rows):
                    fail_units += 1
                    continue
                pairs = [(off, r) for (w, off), r in zip(toks, rows)]
            kind_default = "verse" if u["mode"] == "song" else "prose"
            # word index sorted by offset for range scans
            for pi, (pstream_i, unit_off) in enumerate(paras):
                ch, poff, ptext, pid, layout = stream[pstream_i]
                p_lo, p_hi = unit_off, unit_off + len(ptext)
                kind = kind_default
                lines = []
                for ls, le in split_lines(ptext, kind):
                    a_lo, a_hi = p_lo + ls, p_lo + le
                    lw = [(r, off) for off, r in pairs
                          if a_lo <= off < a_hi]
                    if not lw:
                        continue
                    lines.append({
                        "s": lw[0][0][1], "e": lw[-1][0][2],
                        "t": ptext[ls:le],
                        "w": [[r[1], r[2], off - a_lo,
                               len(toks[[t[1] for t in toks].index(off)][0])
                               if False else 0]  # placeholder, fixed below
                              for r, off in lw],
                    })
                if not lines:
                    continue
                blocks_by_ch[ch].append({
                    "p": pid, "kind": kind, "stream": si, "unit": gid,
                    "s": lines[0]["s"], "e": lines[-1]["e"], "lines": lines})

    # NOTE: word char lengths patched in a second pass (see _patch_word_lens)
    _patch_word_lens(blocks_by_ch, unit_by_id, stream, index, word_rows, routes)

    # order blocks within chapter by (unit, s)
    for ch in blocks_by_ch:
        blocks_by_ch[ch].sort(key=lambda b: (b["unit"], b["s"]))

    # chapter spans: merge consecutive same-stream blocks
    chapters_out = []
    for ch in ch_order:
        blocks = blocks_by_ch.get(ch)
        if not blocks:
            continue
        spans = []
        for b in blocks:
            if spans and spans[-1][0] == b["stream"] and b["s"] - spans[-1][2] < 5000:
                spans[-1][2] = b["e"]
            else:
                spans.append([b["stream"], b["s"], b["e"]])
        for sp in spans:
            sp[2] = min(sp[2] + CHAPTER_PAD_MS, streams_out[sp[0]]["dur"])
        chapters_out.append({"ch": ch, "title": ch_meta[ch]["title"],
                             "dur": 0, "spans": spans})
    # the breath pad must not overlap the NEXT chapter's opening words on the
    # same stream (double-play at auto-advance otherwise)
    for a, b in zip(chapters_out, chapters_out[1:]):
        last, first = a["spans"][-1], b["spans"][0]
        if last[0] == first[0] and last[2] > first[1]:
            last[2] = max(last[1], first[1])
    for c in chapters_out:
        c["dur"] = sum(sp[2] - sp[1] for sp in c["spans"])

    if not chapters_out:
        return None, "no-aligned-chapters"

    # write
    wdir = os.path.join(OUT, slug)
    os.makedirs(os.path.join(wdir, "ch"), exist_ok=True)
    book = {"v": 1, "slug": slug, "streams": streams_out, "chapters": chapters_out}
    json.dump(book, open(os.path.join(wdir, "book.json"), "w"),
              ensure_ascii=False, separators=(",", ":"))
    for ch, blocks in blocks_by_ch.items():
        used = sorted({b["stream"] for b in blocks})
        remap = {si: i for i, si in enumerate(used)}
        payload = {"v": 1, "work": slug, "ch": ch,
                   "streams": [streams_out[si] for si in used],
                   "blocks": [{**{k: v for k, v in b.items() if k != "unit"},
                               "stream": remap[b["stream"]]} for b in blocks]}
        json.dump(payload, open(os.path.join(wdir, "ch", f"{ch}.json"), "w"),
                  ensure_ascii=False, separators=(",", ":"))

    manifest_entry = {
        "dur": sum(c["dur"] for c in chapters_out),
        "tracks": sorted({s["id"].split(":")[0] for s in streams_out}),
        # stream urls ship to the client statically so the player can prime
        # <audio> elements inside the first user gesture, pre-fetch
        "streams": [{"id": s["id"], "url": s["url"]} for s in streams_out],
        "chapters": {c["ch"]: c["dur"] for c in chapters_out},
    }
    if verbose:
        log(f"  {slug}: {len(chapters_out)} chapters, "
            f"{manifest_entry['dur']/3600000:.1f}h"
            + (f", {fail_units} units failed" if fail_units else ""))
    return manifest_entry, None


def _patch_word_lens(blocks_by_ch, unit_by_id, stream, index, word_rows, routes):
    """Fill word char lengths: w rows are [s,e,off,len] with off into line text."""
    # Rebuild: for each block line word, len = length of the token at that offset.
    # Tokens are whitespace-split, so len = distance to next whitespace in line.
    for ch, blocks in blocks_by_ch.items():
        for b in blocks:
            for ln in b["lines"]:
                t = ln["t"]
                for w in ln["w"]:
                    off = w[2]
                    end = off
                    while end < len(t) and not t[end].isspace():
                        end += 1
                    w[3] = end - off


# ── verse-blocks (read-mode styling; no alignment needed) ──────────────────

def emit_verse_blocks():
    """Embedded-verse paragraph ids per PROSE chapter, from dataset modes."""
    out = {}
    works = {w["slug"]: w for w in
             json.load(open(os.path.join(REPO, "classification", "works.json")))}
    for slug, w in sorted(works.items()):
        if w.get("class") != "mixed":
            continue
        ds_path = os.path.join(DATASET, f"{slug}.jsonl")
        if not os.path.exists(ds_path):
            continue
        chapters = load_chapter_paras(slug)
        if not chapters:
            continue
        stream, index, ref_index = build_para_stream(chapters)
        per_ch = defaultdict(list)
        for line in open(ds_path, encoding="utf-8"):
            u = json.loads(line)
            if u["mode"] != "song":
                continue
            paras = unit_paragraphs(u, stream, index, ref_index)
            if paras is None:
                continue
            for pstream_i, _ in paras:
                ch, poff, ptext, pid, layout = stream[pstream_i]
                if layout != "verse" and pid:   # verse inside a prose chapter
                    per_ch[ch].append(pid)
        if per_ch:
            out[slug] = dict(per_ch)
    os.makedirs(SITE_DATA, exist_ok=True)
    path = os.path.join(SITE_DATA, "verse-blocks.json")
    json.dump(out, open(path, "w"), ensure_ascii=False, separators=(",", ":"))
    n = sum(len(v) for v in out.values())
    log(f"verse-blocks: {len(out)} works, {n} chapters -> {path}")


# ── main ───────────────────────────────────────────────────────────────────

def pull_streams():
    os.makedirs(STREAMS_DIR, exist_ok=True)
    r = subprocess.run(["rclone", "copy", f"{BUCKET}/align/_streams/", STREAMS_DIR,
                        "--s3-no-check-bucket", "--transfers", "16"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"pull failed: {r.stderr[-300:]}")
    log(f"pulled {len(os.listdir(STREAMS_DIR))} raw alignments")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--pull", action="store_true")
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--emit-verse-blocks", action="store_true")
    ap.add_argument("--exclude", help="JSON file of slugs to drop from the "
                    "site manifest (works pending TTS regeneration)")
    args = ap.parse_args()

    if args.emit_verse_blocks:
        emit_verse_blocks()
        return
    if args.pull:
        pull_streams()

    works = {w["slug"]: w for w in
             json.load(open(os.path.join(REPO, "classification", "works.json")))}
    targets = [args.work] if args.work else sorted(works) if args.all else []
    if not targets:
        sys.exit("pass --work <slug> or --all")

    manifest, skipped = {}, defaultdict(int)
    for slug in targets:
        wclass = works.get(slug, {}).get("class", "prose")
        try:
            entry, err = merge_work(slug, wclass, verbose=bool(args.work))
        except Exception as ex:
            entry, err = None, f"error:{ex}"
            if args.work:
                raise
        if entry:
            manifest[slug] = entry
        else:
            skipped[err.split(":")[0]] += 1
    log(f"merged {len(manifest)} works; skipped: {dict(skipped)}")

    if args.all:
        if args.exclude:
            drop = set(json.load(open(args.exclude)))
            n0 = len(manifest)
            manifest = {k: v for k, v in manifest.items() if k not in drop}
            log(f"excluded {n0 - len(manifest)} works pending regeneration")
        os.makedirs(SITE_DATA, exist_ok=True)
        path = os.path.join(SITE_DATA, "audio-manifest.json")
        json.dump(manifest, open(path, "w"), ensure_ascii=False,
                  separators=(",", ":"))
        log(f"manifest: {len(manifest)} works -> {path}")

    if args.upload:
        r = subprocess.run(["rclone", "copy", OUT, f"{BUCKET}/align/",
                            "--s3-no-check-bucket", "--transfers", "32"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"upload failed: {r.stderr[-300:]}")
        log("sidecars uploaded")


if __name__ == "__main__":
    main()
