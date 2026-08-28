#!/usr/bin/env python3
"""
Falsafa audiobook worker — runs on a rented GPU pod, self-contained.

Pulls a corpus from R2, narrates its shard with Kokoro, encodes each work into ONE
continuous HLS stream, verifies it, uploads to R2, deletes locally.

Two tracks, selected with --track:
  narration  prose, af_heart, straight through           -> works/<slug>/
  verse      poetry, second voice, line-break prosody    -> verse/<slug>/

Idempotent across the whole fleet: at startup it lists what already exists in R2 and
skips it, so pods can be added/killed/restarted freely and never duplicate work.

  python3 pod_worker.py --shard 0 --of 32 --bucket r2:falsafa-audio
  python3 pod_worker.py --shard 0 --of 8 --track verse --voice bm_george
"""
from __future__ import annotations
import argparse, gzip, json, os, shutil, subprocess, sys, time

SAMPLE_RATE = 24000
LANG = "a"
BITRATE = "32k"
SEGMENT_SEC = 10
SEC_PER_1000_CHARS = 61.4
DURATION_TOLERANCE = (0.70, 1.40)

# Kokoro collapses "\n", so verse read straight through becomes one run-on sentence.
# Verse is synthesised line by line with real rests, which also slows the expected
# audio-per-character rate: the tolerance band below is widened to match.
TRACKS = {
    # ceiling widened 1.40 -> 1.75 after Viramitrodaya (503k chars of IAST-dense
    # translation) rejected at exactly 1.40: it reads at 86 s/1000 chars, not the
    # English-calibrated 61.4, because diacritics and *italic* markers slow the voice.
    # 1.75 still catches genuine runaway loops (they blow up 2-10x); it only rescues
    # slow-but-faithful audio. Takes effect on relaunch, not on the running fleet.
    "narration": {"corpus": "narration_corpus.json.gz", "prefix": "works",
                  "voice": "af_heart", "speed": 1.0, "rate": 61.4, "tol": (0.70, 1.75)},
    # rate is voice-specific: bm_george reads ~20% slower than af_heart at the same
    # speed setting, measured over Ghalib and Shakespeare (83.4 s/1000 chars net of
    # the line rests). The band is wide because works differ enormously in line length
    # -- short-lined lyric pays many rests per character, wrapped-prose verse pays few.
    "verse":     {"corpus": "verse_corpus.json.gz",     "prefix": "verse",
                  "voice": "bm_george", "speed": 0.92, "rate": 83.0, "tol": (0.60, 1.45)},
}
LINE_GAP, STANZA_GAP = 0.32, 0.6


def expected_seconds(w, cfg, track):
    """Verse pays for its rests, and short-lined verse pays a lot of them."""
    sec = w["chars"] / 1000 * cfg["rate"]
    if track == "verse":
        sec += w.get("lines", 0) * LINE_GAP + w.get("blanks", 0) * (STANZA_GAP - LINE_GAP)
    return sec


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def fetch_corpus(bucket, workdir, name):
    local = os.path.join(workdir, name)
    if not os.path.exists(local):
        print("[boot] downloading corpus...", flush=True)
        r = sh(["rclone", "copy", f"{bucket}/_bootstrap/{name}",
                workdir, "--s3-no-check-bucket", "--transfers", "8"])
        if r.returncode != 0:
            sys.exit(f"corpus download failed: {r.stderr[-500:]}")
    with gzip.open(local, "rb") as f:
        works = json.load(f)
    works.sort(key=lambda w: -w["chars"])          # fat tail first
    return works


def already_done(bucket, prefix):
    """One listing call -> the set of works already uploaded (fleet-wide idempotency)."""
    r = sh(["rclone", "lsf", f"{bucket}/{prefix}/", "--dirs-only", "--s3-no-check-bucket"])
    if r.returncode != 0:
        return set()
    return {d.strip("/") for d in r.stdout.split() if d.strip()}


def open_encoder(outdir):
    os.makedirs(outdir, exist_ok=True)
    return subprocess.Popen([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
        "-c:a", "aac", "-b:a", BITRATE, "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-f", "hls", "-hls_time", str(SEGMENT_SEC), "-hls_playlist_type", "vod",
        "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4",
        "-hls_segment_filename", os.path.join(outdir, "seg_%05d.m4s"),
        os.path.join(outdir, "index.m3u8"),
    ], stdin=subprocess.PIPE)


def verify(outdir, expected_sec, tol):
    pl = os.path.join(outdir, "index.m3u8")
    if not os.path.exists(pl):
        return False, "no playlist"
    if not [f for f in os.listdir(outdir) if f.endswith(".m4s")]:
        return False, "no segments"
    r = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", pl])
    try:
        dur = float(r.stdout.strip())
    except ValueError:
        return False, "will not decode"
    if dur <= 0:
        return False, "zero duration"
    ratio = dur / expected_sec if expected_sec else 0
    if not (tol[0] <= ratio <= tol[1]):
        return False, f"duration ratio {ratio:.2f} (got {dur:.0f}s want {expected_sec:.0f}s)"
    r = sh(["ffmpeg", "-v", "info", "-i", pl, "-af", "volumedetect", "-f", "null", "-"])
    mean = None
    for line in r.stderr.splitlines():
        if "mean_volume:" in line:
            mean = float(line.split("mean_volume:")[1].split("dB")[0].strip())
    if mean is None:
        return False, "no level reading"
    if mean < -50:
        return False, f"silent ({mean:.1f} dB)"
    return True, f"{dur/3600:.2f}h ratio={ratio:.2f} {mean:.0f}dB"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int, required=True)
    ap.add_argument("--of", type=int, required=True)
    ap.add_argument("--bucket", default="r2:falsafa-audio")
    ap.add_argument("--workdir", default="/workspace/narr")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--track", choices=list(TRACKS), default="narration")
    ap.add_argument("--voice", default="", help="override the track's default voice")
    ap.add_argument("--slug", default="", help="restrict to one slug (mop-up)")
    ap.add_argument("--tol-max", type=float, default=0.0,
                    help="override the verification ratio ceiling (short fragmented "
                         "Indic works read slower than the char-rate model predicts)")
    ap.add_argument("--calibrate", action="store_true",
                    help="run a few works, report RTF, upload nothing")
    args = ap.parse_args()

    cfg = TRACKS[args.track]
    if args.tol_max:
        cfg = {**cfg, "tol": (cfg["tol"][0], args.tol_max)}
    voice, speed, prefix = args.voice or cfg["voice"], cfg["speed"], cfg["prefix"]

    os.makedirs(args.workdir, exist_ok=True)
    works = fetch_corpus(args.bucket, args.workdir, cfg["corpus"])
    done = set() if args.calibrate else already_done(args.bucket, prefix)

    mine = [w for i, w in enumerate(works) if i % args.of == args.shard and w["slug"] not in done]
    if args.slug:
        mine = [w for w in works if w["slug"] == args.slug and w["slug"] not in done]
    if args.limit:
        mine = mine[:args.limit]

    tot_chars = sum(w["chars"] for w in mine)
    print(f"[shard {args.shard}/{args.of}] track={args.track} voice={voice} "
          f"{len(mine)} works, "
          f"{tot_chars/1000*SEC_PER_1000_CHARS/3600:.1f} audio-hours to make", flush=True)

    import torch
    from kokoro import KPipeline
    import numpy as np
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    pipe = KPipeline(lang_code=LANG, device=dev)
    print(f"[tts] kokoro on {dev} "
          f"({torch.cuda.get_device_name(0) if dev=='cuda' else 'cpu'})", flush=True)

    def synth(text):
        """One unit -> float32 PCM. Verse is read line by line with rests between."""
        pieces = text.split("\n") if args.track == "verse" else [text]
        chunks = []
        for piece in pieces:
            piece = piece.strip()
            if args.track == "verse" and not piece:
                chunks.append(np.zeros(int(SAMPLE_RATE * STANZA_GAP), dtype="float32"))
                continue
            if not piece:
                continue
            for _, _, audio in pipe(piece, voice=voice, speed=speed):
                if audio is None:
                    continue
                a = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
                chunks.append(a.astype("float32").reshape(-1))
            if args.track == "verse":
                chunks.append(np.zeros(int(SAMPLE_RATE * LINE_GAP), dtype="float32"))
        return np.concatenate(chunks) if chunks else None

    ok = bad = 0
    audio_total = 0.0
    t_start = time.time()
    for n, w in enumerate(mine):
        outdir = os.path.join(args.workdir, w["slug"])
        shutil.rmtree(outdir, ignore_errors=True)
        expected = expected_seconds(w, cfg, args.track)
        t0 = time.time()
        enc = open_encoder(outdir)
        produced = 0.0
        try:
            for text in w["units"]:
                buf = synth(text)
                if buf is None:
                    continue
                produced += buf.size / SAMPLE_RATE
                enc.stdin.write(buf.tobytes())
            enc.stdin.close()
            enc.wait(timeout=900)
        except Exception as e:
            try: enc.kill()
            except Exception: pass
            print(f"  FAIL {w['slug'][:40]}: {e}", flush=True)
            bad += 1
            shutil.rmtree(outdir, ignore_errors=True)
            continue

        good, report = verify(outdir, expected, cfg["tol"])
        gen = time.time() - t0
        if not good:
            print(f"  REJECT {w['slug'][:40]}: {report}", flush=True)
            bad += 1
            shutil.rmtree(outdir, ignore_errors=True)
            continue

        if not args.calibrate:
            r = sh(["rclone", "copy", outdir, f"{args.bucket}/{prefix}/{w['slug']}/",
                    "--transfers", "32", "--checkers", "32", "--s3-no-check-bucket",
                    "--retries", "3"])
            if r.returncode != 0:
                print(f"  UPLOAD-FAIL {w['slug'][:40]}: {r.stderr[-200:]}", flush=True)
                bad += 1
                shutil.rmtree(outdir, ignore_errors=True)
                continue

        shutil.rmtree(outdir, ignore_errors=True)
        ok += 1
        audio_total += produced
        el = time.time() - t_start
        print(f"  [{n+1}/{len(mine)}] {w['title'][:36]:36} {report} "
              f"rtf={produced/gen:.0f}x cum_rtf={audio_total/el:.0f}x", flush=True)

    el = time.time() - t_start
    print(f"\nSHARD_DONE shard={args.shard} ok={ok} bad={bad} "
          f"audio_hours={audio_total/3600:.2f} wall_hours={el/3600:.2f} "
          f"RTF={audio_total/el if el else 0:.1f}x", flush=True)


if __name__ == "__main__":
    main()
