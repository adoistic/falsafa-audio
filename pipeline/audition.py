#!/usr/bin/env python3
"""
Voice audition for the VERSE half of the corpus.

Narration is locked to af_heart. Verse wants a different voice so the two experiences
are audibly distinct, plus verse-specific prosody: a real pause at every line break
(Kokoro collapses "\n", so without this a sonnet reads as one long run-on sentence)
and a slightly slower rate.

  python3 pipeline/audition.py                 # render the shortlist
  python3 pipeline/audition.py --voices bm_george,af_bella --speed 0.85
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys

SAMPLE_RATE = 24000
OUT = "/tmp/verse_audition"

SHORTLIST = ["bm_george", "bm_fable", "bf_emma", "am_michael", "am_fenrir", "af_bella"]

EXCERPTS = {
    "ghalib": ("mirza-ghalib-diwan-e-ghalib-74ed4c", 12),
    "shakespeare": ("william-shakespeare-sonnets-65f289", 14),
}


def verse_lines(text):
    """Verse renders line-by-line; blank lines become longer rests."""
    return [ln.strip() for ln in text.split("\n")]


def render(pipe, np, text, voice, speed, line_gap=0.32, stanza_gap=0.6):
    chunks = []
    for ln in verse_lines(text):
        if not ln:
            chunks.append(np.zeros(int(SAMPLE_RATE * stanza_gap), dtype="float32"))
            continue
        for _, _, audio in pipe(ln, voice=voice, speed=speed):
            if audio is None:
                continue
            a = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
            chunks.append(a.astype("float32").reshape(-1))
        chunks.append(np.zeros(int(SAMPLE_RATE * line_gap), dtype="float32"))
    return np.concatenate(chunks) if chunks else np.zeros(1, dtype="float32")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--voices", default=",".join(SHORTLIST))
    ap.add_argument("--speed", type=float, default=0.92)
    ap.add_argument("--lines", type=int, default=14, help="lines of each excerpt to read")
    args = ap.parse_args()

    import numpy as np, soundfile as sf, torch
    from kokoro import KPipeline
    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    pipe = KPipeline(lang_code="a", device=dev)

    os.makedirs(OUT, exist_ok=True)
    texts = {}
    for name, (slug, _) in EXCERPTS.items():
        for line in open(f"dataset/{slug}.jsonl", encoding="utf-8"):
            d = json.loads(line)
            if d["mode"] == "song":
                texts[name] = "\n".join(d["text"].split("\n")[:args.lines])
                break

    for voice in args.voices.split(","):
        for name, text in texts.items():
            path = f"{OUT}/{name}__{voice}.wav"
            try:
                sf.write(path, render(pipe, np, text, voice, args.speed), SAMPLE_RATE)
                print(f"  {path}", flush=True)
            except Exception as e:
                print(f"  FAIL {voice} {name}: {e}", flush=True)
    print(f"\nwrote to {OUT}")


if __name__ == "__main__":
    main()
