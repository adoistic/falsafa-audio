#!/usr/bin/env python3
"""
Falsafa audiobook narrator — TTS a work's narration units into ONE continuous
HLS stream, verify it, upload to R2, delete locally.

Design notes:
  * One HLS playlist PER WORK (not per unit): a listener plays a book, not 232 clips.
    Units are streamed through a single long-running ffmpeg process, so memory stays
    bounded regardless of book length (Pliny = 80h audio = ~330MB, never held in RAM).
  * Idempotent: a work is skipped if its done-marker already exists. Safe to re-run,
    safe to shard across N GPUs, safe to kill and resume.
  * Verified before upload: duration-vs-expected ratio + silence/level checks catch the
    failure modes that matter (truncation, repetition loops, silent output).

Backends: `kokoro` (PyTorch; CPU/CUDA/MPS) — the portable path used on RunPod.

Usage:
  python3 pipeline/narrate.py --shard 0 --of 4 --workdir /tmp/narr --r2 r2:falsafa-audio
  python3 pipeline/narrate.py --limit 2 --no-upload      # local smoke test
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, time, shutil, math

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DATASET = os.path.join(REPO, "dataset")
CLASSIFICATION = os.path.join(REPO, "classification")

SAMPLE_RATE = 24000
VOICE = "af_heart"          # locked: chosen by ear over the alternatives
LANG = "a"                  # American English
BITRATE = "32k"             # mono speech; ~16 MB/hour, ~96 GB for the full corpus
SEGMENT_SEC = 10

# measured on real corpus text: audio-seconds produced per 1000 characters
SEC_PER_1000_CHARS = 61.4
# a work whose audio falls outside this band vs expectation is suspect
DURATION_TOLERANCE = (0.70, 1.40)


# ─────────────────────────────────────────────────────────── work list

def narration_works():
    """Every work needing narration (prose + the prose half of mixed), longest first
    so the fat tail starts early and stragglers don't dominate the tail of the run."""
    works = {w["slug"]: w for w in json.load(open(f"{CLASSIFICATION}/works.json"))}
    out = []
    for slug, w in works.items():
        if w.get("class") not in ("prose", "mixed"):
            continue
        path = f"{DATASET}/{slug}.jsonl"
        if not os.path.exists(path):
            continue
        units, chars = [], 0
        for line in open(path, encoding="utf-8"):
            u = json.loads(line)
            if u["mode"] == "narration":
                units.append(u)
                chars += u["chars"]
        if units:
            out.append({"slug": slug, "title": units[0]["title"], "units": units, "chars": chars})
    out.sort(key=lambda x: -x["chars"])
    return out


# ─────────────────────────────────────────────────────────── TTS backend

class KokoroTTS:
    """PyTorch Kokoro. Yields float32 PCM at 24kHz for a chunk of text."""

    def __init__(self, device=None):
        from kokoro import KPipeline
        import torch
        if device is None:
            device = ("cuda" if torch.cuda.is_available()
                      else "mps" if torch.backends.mps.is_available() else "cpu")
        self.device = device
        self.pipe = KPipeline(lang_code=LANG, device=device)
        print(f"[tts] kokoro on {device}", flush=True)

    def synth(self, text: str):
        import numpy as np
        chunks = []
        for _, _, audio in self.pipe(text, voice=VOICE, speed=1.0):
            if audio is None:
                continue
            a = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
            chunks.append(a.astype("float32").reshape(-1))
        if not chunks:
            return None
        return np.concatenate(chunks)


# ─────────────────────────────────────────────────────────── encode + verify

def open_hls_encoder(outdir: str):
    """Long-running ffmpeg reading raw f32 PCM on stdin, emitting HLS fMP4 segments."""
    os.makedirs(outdir, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
        "-c:a", "aac", "-b:a", BITRATE, "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-f", "hls", "-hls_time", str(SEGMENT_SEC), "-hls_playlist_type", "vod",
        "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4",
        "-hls_segment_filename", os.path.join(outdir, "seg_%05d.m4s"),
        os.path.join(outdir, "index.m3u8"),
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)


def probe_duration(path: str) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", path], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def verify_work(outdir: str, expected_sec: float):
    """Cheap, runs on 100% of works. Returns (ok, report)."""
    playlist = os.path.join(outdir, "index.m3u8")
    if not os.path.exists(playlist):
        return False, "no playlist"
    segs = [f for f in os.listdir(outdir) if f.endswith(".m4s")]
    if not segs:
        return False, "no segments"

    dur = probe_duration(playlist)
    if dur <= 0:
        return False, "playlist will not decode"

    ratio = dur / expected_sec if expected_sec else 0
    if not (DURATION_TOLERANCE[0] <= ratio <= DURATION_TOLERANCE[1]):
        return False, f"duration {dur:.0f}s vs expected {expected_sec:.0f}s (ratio {ratio:.2f})"

    # level check — catches silent/garbage output
    r = subprocess.run(["ffmpeg", "-v", "info", "-i", playlist, "-af", "volumedetect",
                        "-f", "null", "-"], capture_output=True, text=True)
    mean = None
    for line in r.stderr.splitlines():
        if "mean_volume:" in line:
            mean = float(line.split("mean_volume:")[1].split("dB")[0].strip())
    if mean is None:
        return False, "no level reading"
    if mean < -50:
        return False, f"audio essentially silent (mean {mean:.1f} dB)"

    return True, f"{dur/3600:.2f}h, {len(segs)} segs, mean {mean:.1f}dB, ratio {ratio:.2f}"


def upload(outdir: str, remote: str, slug: str):
    dest = f"{remote}/works/{slug}/"
    r = subprocess.run(["rclone", "copy", outdir, dest,
                        "--transfers", "32", "--checkers", "32",
                        "--s3-no-check-bucket", "--retries", "3"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"rclone failed: {r.stderr[-400:]}")


# ─────────────────────────────────────────────────────────── main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--of", type=int, default=1)
    ap.add_argument("--workdir", default="/tmp/falsafa-narrate")
    ap.add_argument("--r2", default="r2:falsafa-audio", help="rclone remote:bucket")
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--slug", default=None, help="narrate one specific work (testing/retry)")
    ap.add_argument("--keep", action="store_true", help="keep local output (don't delete after)")
    ap.add_argument("--state", default=None, help="dir for done-markers")
    args = ap.parse_args()

    state = args.state or os.path.join(args.workdir, "state")
    os.makedirs(state, exist_ok=True)

    allworks = narration_works()
    if args.slug:
        mine = [w for w in allworks if w["slug"] == args.slug]
        if not mine:
            sys.exit(f"no such work: {args.slug}")
    else:
        mine = [w for i, w in enumerate(allworks) if i % args.of == args.shard]
        if args.limit:
            mine = mine[:args.limit]       # longest first (fat tail early)
    total_chars = sum(w["chars"] for w in mine)
    print(f"[shard {args.shard}/{args.of}] {len(mine)} works, {total_chars:,} chars, "
          f"~{total_chars/1000*SEC_PER_1000_CHARS/3600:.2f} audio-hours", flush=True)

    tts = KokoroTTS()
    import numpy as np

    done = failed = 0
    t_start = time.time()
    for wi, w in enumerate(mine):
        marker = os.path.join(state, f"{w['slug']}.done")
        if os.path.exists(marker):
            continue

        outdir = os.path.join(args.workdir, w["slug"])
        shutil.rmtree(outdir, ignore_errors=True)
        expected = w["chars"] / 1000 * SEC_PER_1000_CHARS

        t0 = time.time()
        enc = open_hls_encoder(outdir)
        produced = 0.0
        try:
            for u in w["units"]:
                audio = tts.synth(u["text"])
                if audio is None or audio.size == 0:
                    continue
                produced += audio.size / SAMPLE_RATE
                enc.stdin.write(audio.tobytes())
            enc.stdin.close()
            enc.wait(timeout=600)
        except Exception as e:
            try:
                enc.kill()
            except Exception:
                pass
            print(f"  FAIL {w['slug']}: {e}", flush=True)
            failed += 1
            continue

        ok, report = verify_work(outdir, expected)
        gen = time.time() - t0
        if not ok:
            print(f"  REJECT {w['slug'][:44]}: {report}", flush=True)
            failed += 1
            shutil.rmtree(outdir, ignore_errors=True)
            continue

        if not args.no_upload:
            try:
                upload(outdir, args.r2, w["slug"])
            except Exception as e:
                print(f"  UPLOAD-FAIL {w['slug']}: {e}", flush=True)
                failed += 1
                continue

        open(marker, "w").write(json.dumps({"slug": w["slug"], "report": report,
                                            "gen_sec": round(gen, 1)}))
        if not args.keep:
            shutil.rmtree(outdir, ignore_errors=True)
        done += 1
        rtf = produced / gen if gen else 0
        elapsed = time.time() - t_start
        print(f"  [{wi+1}/{len(mine)}] {w['title'][:40]:40} {report}  rtf={rtf:.0f}x  "
              f"elapsed={elapsed/3600:.2f}h", flush=True)

    print(f"\nshard {args.shard}: done={done} failed={failed} "
          f"wall={(time.time()-t_start)/3600:.2f}h", flush=True)


if __name__ == "__main__":
    main()
