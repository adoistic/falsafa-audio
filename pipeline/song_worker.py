#!/usr/bin/env python3
"""
Falsafa song worker — the curated "creative song" layer.

Runs ACE-Step 1.5 XL (MIT) on a GPU pod over highlights/selections.json: one song per
curated passage, encoded to HLS like everything else, uploaded to R2, deleted locally.

Deliberately NOT the whole corpus. Every open song model measures 35-51% lyric WER on
contamination-free benchmarks against our narration's 3.2%, so singing is reserved for
a few hundred hand-picked passages where the payoff is worth the risk, and every work
still has a faithful spoken reading of the same text on the verse track.

  python3 song_worker.py --shard 0 --of 4 --bucket r2:falsafa-audio
  python3 song_worker.py --shard 0 --of 1 --limit 6 --keep   # pilot, no upload

The ACE-Step API surface here follows docs/en/INFERENCE.md upstream; --probe prints what
the installed package actually exposes so a version drift is visible before a long run.
"""
from __future__ import annotations
import argparse, json, os, shutil, subprocess, sys, time

SAMPLE_RATE = 44100          # ACE-Step is a music model; keep its native rate
BITRATE = "96k"              # music, not speech
SEGMENT_SEC = 10
CONFIG = "acestep-v15-xl-sft"
LM_MODEL = "acestep-5Hz-lm-0.6B"
STEPS = 32                   # 8 (turbo default) is audibly worse; 32 is the sweet spot
SEC_PER_CHAR = 1 / 3.0       # sung lyrics run far slower than speech
MIN_SEC, MAX_SEC = 90, 240


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def fetch(bucket, workdir, name):
    local = os.path.join(workdir, name)
    if not os.path.exists(local):
        r = sh(["rclone", "copy", f"{bucket}/_bootstrap/{name}", workdir,
                "--s3-no-check-bucket"])
        if r.returncode != 0:
            sys.exit(f"download of {name} failed: {r.stderr[-400:]}")
    return local


def already_done(bucket):
    r = sh(["rclone", "lsf", f"{bucket}/songs/", "--dirs-only", "--recursive",
            "--s3-no-check-bucket"])
    if r.returncode != 0:
        return set()
    return {d.strip("/") for d in r.stdout.split() if d.count("/") == 2}


def lyric_block(text):
    """ACE-Step reads structure tags; untagged lyrics get an arbitrary structure."""
    stanzas = [s.strip() for s in text.split("\n\n") if s.strip()]
    if len(stanzas) <= 1:
        return "[verse]\n" + text.strip()
    tags = ["[verse]", "[chorus]", "[verse]", "[bridge]", "[outro]"]
    return "\n\n".join(f"{tags[i % len(tags)]}\n{s}" for i, s in enumerate(stanzas))


def encode_hls(wav, outdir):
    os.makedirs(outdir, exist_ok=True)
    r = sh(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", wav,
            "-c:a", "aac", "-b:a", BITRATE, "-ar", str(SAMPLE_RATE),
            "-f", "hls", "-hls_time", str(SEGMENT_SEC), "-hls_playlist_type", "vod",
            "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4",
            "-hls_segment_filename", os.path.join(outdir, "seg_%05d.m4s"),
            os.path.join(outdir, "index.m3u8")])
    return r.returncode == 0


def verify(outdir, want_sec):
    pl = os.path.join(outdir, "index.m3u8")
    if not os.path.exists(pl) or not [f for f in os.listdir(outdir) if f.endswith(".m4s")]:
        return False, "no segments"
    r = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", pl])
    try:
        dur = float(r.stdout.strip())
    except ValueError:
        return False, "will not decode"
    if dur < 20:
        return False, f"too short ({dur:.0f}s)"
    r = sh(["ffmpeg", "-v", "info", "-i", pl, "-af", "volumedetect", "-f", "null", "-"])
    mean = next((float(l.split("mean_volume:")[1].split("dB")[0])
                 for l in r.stderr.splitlines() if "mean_volume:" in l), None)
    if mean is None:
        return False, "no level reading"
    if mean < -45:
        return False, f"silent ({mean:.1f} dB)"
    return True, f"{dur:.0f}s (want {want_sec:.0f}s) {mean:.0f}dB"


def probe():
    import acestep, inspect
    from acestep.inference import GenerationParams, GenerationConfig
    print("acestep", getattr(acestep, "__version__", "?"), acestep.__file__)
    for cls in (GenerationParams, GenerationConfig):
        print(f"\n{cls.__name__}:")
        for f in getattr(cls, "__dataclass_fields__", {}) or inspect.signature(cls).parameters:
            print("   ", f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--of", type=int, default=1)
    ap.add_argument("--bucket", default="r2:falsafa-audio")
    ap.add_argument("--workdir", default="/workspace/song")
    ap.add_argument("--checkpoints", default="/workspace/checkpoints")
    ap.add_argument("--project-root", default="/workspace/ACE-Step-1.5")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--keep", action="store_true", help="keep wavs locally, skip upload")
    ap.add_argument("--probe", action="store_true", help="print the installed API surface")
    args = ap.parse_args()

    if args.probe:
        return probe()

    os.makedirs(args.workdir, exist_ok=True)
    sels = json.load(open(fetch(args.bucket, args.workdir, "song_selections.json")))
    done = set() if args.keep else already_done(args.bucket)

    mine = [s for i, s in enumerate(sels)
            if i % args.of == args.shard and f"{s['slug']}/{s['cand']}" not in done]
    if args.limit:
        mine = mine[:args.limit]
    print(f"[shard {args.shard}/{args.of}] {len(mine)} songs to make", flush=True)

    from acestep.handler import AceStepHandler
    from acestep.llm_inference import LLMHandler
    from acestep.inference import GenerationParams, GenerationConfig, generate_music

    dit = AceStepHandler()
    dit.initialize_service(project_root=args.project_root, config_path=CONFIG, device="cuda")
    llm = LLMHandler()
    llm.initialize(checkpoint_dir=args.checkpoints, lm_model_path=LM_MODEL,
                   backend="vllm", device="cuda")
    print(f"[song] {CONFIG} ready", flush=True)

    ok = bad = 0
    t_start = time.time()
    for n, s in enumerate(mine):
        key = f"{s['slug']}/{s['cand']}"
        raw = os.path.join(args.workdir, "raw", key.replace("/", "_"))
        outdir = os.path.join(args.workdir, "hls", key.replace("/", "_"))
        shutil.rmtree(raw, ignore_errors=True); shutil.rmtree(outdir, ignore_errors=True)
        want = min(MAX_SEC, max(MIN_SEC, len(s["lyrics"]) * SEC_PER_CHAR))
        t0 = time.time()
        try:
            res = generate_music(
                dit, llm,
                GenerationParams(caption=s["style"], lyrics=lyric_block(s["lyrics"]),
                                 duration=want, inference_steps=STEPS, seed=1729),
                GenerationConfig(batch_size=1, audio_format="wav"),
                save_dir=raw)
        except Exception as e:
            print(f"  FAIL {key}: {type(e).__name__} {e}", flush=True); bad += 1; continue
        if not getattr(res, "success", False) or not getattr(res, "audios", None):
            print(f"  FAIL {key}: generation reported no audio", flush=True); bad += 1; continue

        wav = res.audios[0]["path"]
        if not encode_hls(wav, outdir):
            print(f"  FAIL {key}: encode", flush=True); bad += 1; continue
        good, report = verify(outdir, want)
        if not good:
            print(f"  REJECT {key}: {report}", flush=True); bad += 1
            shutil.rmtree(outdir, ignore_errors=True); continue

        json.dump({k: s[k] for k in ("slug", "title", "author", "language", "era",
                                     "cand", "at", "style", "why", "lyrics")},
                  open(os.path.join(outdir, "meta.json"), "w"), ensure_ascii=False, indent=1)

        if not args.keep:
            r = sh(["rclone", "copy", outdir, f"{args.bucket}/songs/{key}/",
                    "--transfers", "16", "--s3-no-check-bucket", "--retries", "3"])
            if r.returncode != 0:
                print(f"  UPLOAD-FAIL {key}: {r.stderr[-200:]}", flush=True); bad += 1; continue
            shutil.rmtree(outdir, ignore_errors=True)
        shutil.rmtree(raw, ignore_errors=True)
        ok += 1
        print(f"  [{n+1}/{len(mine)}] {s['title'][:30]:30} {s['cand']} {report} "
              f"gen={time.time()-t0:.0f}s", flush=True)

    print(f"\nSHARD_DONE shard={args.shard} ok={ok} bad={bad} "
          f"wall_hours={(time.time()-t_start)/3600:.2f}", flush=True)


if __name__ == "__main__":
    main()
