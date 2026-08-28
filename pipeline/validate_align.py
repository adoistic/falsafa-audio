#!/usr/bin/env python3
"""
Spot-check a stream alignment: cut audio at the claimed word timestamps and
greedy-CTC-decode each clip with the same MMS model. If the alignment is
right, the decode of [start,end] reproduces the word (fuzzy — CTC spelling).
Also reports timing sanity (monotonicity, word durations, gaps).

  python3 validate_align.py --track narration --slug <slug> [--n 12]
"""
from __future__ import annotations
import argparse, difflib, gzip, json, os, random, subprocess

import numpy as np
import torch, torchaudio

from align_common import tokenize, normalize_word
from align_worker import TRACKS, BUCKET, SR, decode_stream, load_corpus, rclone

random.seed(17)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--track", required=True, choices=["narration", "verse"])
    ap.add_argument("--slug", required=True)
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--workdir", default="/tmp/align_work")
    args = ap.parse_args()

    prefix = TRACKS[args.track]
    name = f"{prefix}__{args.slug}.json.gz"
    local = os.path.join(args.workdir, name)
    if not os.path.exists(local):
        rclone("copy", f"{BUCKET}/align/_streams/{name}", args.workdir)
    res = json.load(gzip.open(local, "rt"))

    corpus = load_corpus(args.track, args.workdir)
    work = next(w for w in corpus if w["slug"] == args.slug)
    words = []
    for ui, text in enumerate(work["units"]):
        for w, off in tokenize(text):
            words.append((ui, w))
    rows = res["words"]
    print(f"{args.slug}: {len(rows)} aligned / {len(words)} tokens, "
          f"dur {res['duration_ms']/60000:.1f}m, qc {res['qc']}")

    # timing sanity
    bad_mono = sum(1 for a, b in zip(rows, rows[1:]) if b[1] < a[1])
    durs = [r[2] - r[1] for r in rows]
    print(f"monotonicity violations: {bad_mono}; word dur ms "
          f"p50={int(np.percentile(durs,50))} p95={int(np.percentile(durs,95))} "
          f"max={max(durs)}")

    # audio
    stream_dir = os.path.join(args.workdir, "stream_v")
    subprocess.run(["rm", "-rf", stream_dir])
    rclone("copy", f"{BUCKET}/{prefix}/{args.slug}/", stream_dir)
    pcm_path = os.path.join(args.workdir, "validate.pcm")
    decode_stream(stream_dir, pcm_path)
    pcm = np.memmap(pcm_path, dtype=np.int16, mode="r")

    bundle = torchaudio.pipelines.MMS_FA
    model = bundle.get_model(with_star=False).eval()
    labels = bundle.get_labels(star=None)

    def decode_clip(s_ms, e_ms, pad_ms=40):
        s = max(0, int((s_ms - pad_ms) * SR / 1000))
        e = min(len(pcm), int((e_ms + pad_ms) * SR / 1000))
        clip = pcm[s:e].astype(np.float32) / 32768.0
        if len(clip) < 400:
            clip = np.pad(clip, (0, 400 - len(clip)))
        with torch.inference_mode():
            em, _ = model(torch.from_numpy(clip).unsqueeze(0))
        ids = em[0].argmax(-1).tolist()
        out, prev = [], -1
        for i in ids:
            if i != prev and i != 0:
                out.append(labels[i])
            prev = i
        return "".join(out)

    # Spot-check SPANS of ~7 consecutive words: isolated 200ms clips decode
    # poorly regardless of alignment quality (no acoustic context), but a
    # correctly-aligned multi-word span decodes to its text. This is also the
    # granularity read-along actually uses.
    span_n = 7
    starts = [i for i in range(len(rows) - span_n)
              if words[i][0] == words[i + span_n][0]]      # same unit
    picks = sorted(random.sample(starts, min(args.n, len(starts))))
    ok = 0
    for i in picks:
        s, e = rows[i][1], rows[i + span_n - 1][2]
        got = decode_clip(s, e, pad_ms=60)
        want = "".join(normalize_word(w) for _, w in words[i:i + span_n])
        ratio = difflib.SequenceMatcher(None, want, got).ratio()
        good = ratio >= 0.7
        ok += good
        text = " ".join(w for _, w in words[i:i + span_n])
        print(f"  {'OK ' if good else 'MISS'} @{s/1000:7.1f}s ({ratio:.2f}) "
              f"'{text}' -> '{got}'")
    print(f"{ok}/{len(picks)} spans matched")
    # word-boundary precision: decode each span twice, shifted ±120ms; a
    # correct alignment should DEGRADE when shifted (proves we're not just
    # inside a tolerant region)
    i = picks[len(picks) // 2]
    s, e = rows[i][1], rows[i + span_n - 1][2]
    want = "".join(normalize_word(w) for _, w in words[i:i + span_n])
    for shift in (-350, 0, 350):
        got = decode_clip(s + shift, e + shift, pad_ms=0)
        r = difflib.SequenceMatcher(None, want, got).ratio()
        print(f"  shift {shift:+d}ms ratio {r:.2f}")


if __name__ == "__main__":
    main()
