#!/usr/bin/env python3
"""
Forced-alignment worker: HLS stream + known text -> per-word timestamps.

For each (track, slug) stream in falsafa-audio, produces
  align/_streams/<prefix>__<slug>.json.gz
    {"slug", "track", "tokenizer", "duration_ms", "rate_spc",
     "words": [[unit, start_ms, end_ms, score_x1000], ...],   # one row per
     "qc": {...}}                                             # tokenize() token

Method: MMS_FA (wav2vec2 CTC) emissions + torchaudio forced_align, run as a
STREAMING WINDOWED pass — a full-file Viterbi over a 50-hour stream is
quadratic-memory-infeasible, so we keep an (audio cursor, word cursor) pair,
align an ~8-minute audio window against a slightly over-provisioned text
window, keep only words that end well before the window edge (the confident
zone), and advance both cursors to the last kept word. The text window is
sized from the stream's own global seconds-per-char (duration / chars), which
is exact by construction, and refined from observed alignment as we go.

Text is ground truth (never ASR): mistranscription is impossible; only timing
is solved. Words that normalize to nothing (digits handled via num2words,
symbols, non-Latin) align as <star> tokens so every token still gets a span.

Usage:
  python3 align_worker.py --track verse --slug <slug>            # one stream
  python3 align_worker.py --track both --shard 0 --of 8          # fleet mode
Env: R2 creds via RCLONE_CONFIG_R2_* (same as pod_worker.py).
"""
from __future__ import annotations
import argparse, gzip, json, os, shutil, subprocess, sys, time

import numpy as np
import torch, torchaudio

from align_common import TOKENIZER_V, tokenize, normalize_word

SR = 16000
FRAME_SEC = 0.02          # MMS stride 320 samples @16k
CHUNK_SEC = 30            # emission sub-chunk
EMIT_BATCH = 8            # sub-chunks per model batch (GPU throughput)
WINDOW_SEC = 480          # alignment window (8 min)
SAFETY_SEC = 45           # confident zone = window minus this tail
TEXT_OVERPROVISION = 1.08 # feed slightly more text than the window should hold
MIN_WINDOW_KEEP = 5       # if fewer words kept, something is wrong -> shrink/advance

TRACKS = {"narration": "works", "verse": "verse"}
BUCKET = "r2:falsafa-audio"

log = lambda *a: (print(f"[{time.strftime('%H:%M:%S')}]", *a), sys.stdout.flush())


# ── R2 helpers ──────────────────────────────────────────────────────────────

def rclone(*args, check=True):
    r = subprocess.run(["rclone", *args, "--s3-no-check-bucket"],
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"rclone {' '.join(args[:3])}: {r.stderr[-300:]}")
    return r


def load_corpus(track: str, cache_dir: str):
    name = f"{track}_corpus.json.gz"
    path = os.path.join(cache_dir, name)
    if not os.path.exists(path):
        rclone("copy", f"{BUCKET}/_bootstrap/{name}", cache_dir)
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def already_done() -> set:
    r = rclone("lsf", f"{BUCKET}/align/_streams/", check=False)
    return {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}


# ── model ───────────────────────────────────────────────────────────────────

class Aligner:
    def __init__(self, device: str):
        self.device = torch.device(device)
        bundle = torchaudio.pipelines.MMS_FA
        # with_star=False + manual star column: torchaudio's with_star wrapper
        # hardcodes batch size 1 (star_dim = zeros((1, T, 1))) and explodes on
        # batched emissions. We append the same zeros-logit column ourselves,
        # batch-safely, in emissions().
        self.model = bundle.get_model(with_star=False).to(self.device).eval()
        self.dict = bundle.get_dict(star="*")
        self.star = self.dict["*"]

    def tokens_for(self, words):
        """[(norm_string, ...)] -> flat targets + per-word token counts."""
        targets, counts = [], []
        for w in words:
            toks = [self.dict[c] for c in w if c in self.dict] if w else []
            if not toks:
                toks = [self.star]
            targets.extend(toks)
            counts.append(len(toks))
        return targets, counts

    @torch.inference_mode()
    def emissions(self, samples: np.ndarray):
        """f32 mono 16k samples -> ([T, V] log-probs, frame_times[T] seconds).

        Audio is chunked for the transformer; each chunk yields slightly fewer
        frames than samples/320 (conv edges), so frame->time must be mapped
        PER CHUNK, not by uniform scaling — uniform mapping drifts ~20-30 ms
        near chunk boundaries, a third of the accuracy budget.

        Chunks run through the model in batches (equal-length, so no padding
        bookkeeping except the final short chunk, which runs alone).
        """
        n = len(samples)
        step = CHUNK_SEC * SR
        starts = list(range(0, n, step))
        full = [i for i in starts if n - i >= step]
        tail = [i for i in starts if n - i < step]
        outs, times = {}, {}

        for bi in range(0, len(full), EMIT_BATCH):
            batch = full[bi:bi + EMIT_BATCH]
            wav = torch.from_numpy(
                np.stack([samples[i:i + step] for i in batch])).to(self.device)
            em, _ = self.model(wav)
            for j, i in enumerate(batch):
                nf = em.shape[1]
                outs[i] = em[j]
                times[i] = i / SR + np.arange(nf, dtype=np.float64) * FRAME_SEC
        for i in tail:
            chunk = samples[i:]
            if len(chunk) < SR // 4:  # <0.25s tail: pad
                chunk = np.pad(chunk, (0, SR // 4 - len(chunk)))
            wav = torch.from_numpy(chunk).unsqueeze(0).to(self.device)
            em, _ = self.model(wav)
            nf = em.shape[1]
            outs[i] = em[0]
            times[i] = i / SR + np.arange(nf, dtype=np.float64) * FRAME_SEC

        e = torch.cat([outs[i] for i in starts], dim=0)
        # star column (zeros logit, same value torchaudio's with_star appends)
        e = torch.cat([e, e.new_zeros(e.shape[0], 1)], dim=-1)
        t = np.concatenate([times[i] for i in starts])
        return torch.log_softmax(e, dim=-1), t

    @torch.inference_mode()
    def align_em(self, em, frame_times, norm_words):
        """Align precomputed emissions against words.
        Returns per-word (s_sec, e_sec, score) list, or None if infeasible."""
        T = em.shape[0]
        targets, counts = self.tokens_for(norm_words)
        if len(targets) >= T:  # more tokens than frames — caller shrinks text
            return None
        tgt = torch.tensor([targets], dtype=torch.int32, device=self.device)
        try:
            labels, scores = torchaudio.functional.forced_align(
                em.unsqueeze(0), tgt, blank=0)
        except Exception as ex:
            log(f"    forced_align error: {ex}")
            return None
        labels, scores = labels[0], scores[0].exp()
        spans = torchaudio.functional.merge_tokens(labels, scores)  # TokenSpans
        spans = [s for s in spans if s.token != 0]
        if len(spans) != len(targets):
            # would break word bookkeeping — bail (CTC guarantees this fits;
            # a mismatch means something upstream is wrong)
            log(f"    span/target mismatch {len(spans)} != {len(targets)}")
            return None
        out, i = [], 0
        for c in counts:
            ws, we = spans[i], spans[i + c - 1]
            sc = float(np.mean([s.score for s in spans[i:i + c]]))
            out.append((float(frame_times[ws.start]),
                        float(frame_times[min(we.end, T - 1)]) + FRAME_SEC, sc))
            i += c
        return out


# ── stream processing ───────────────────────────────────────────────────────

def decode_stream(stream_dir: str, out_pcm: str) -> float:
    """HLS dir -> mono s16 16k raw pcm on disk. Returns duration seconds."""
    m3u8 = os.path.join(stream_dir, "index.m3u8")
    r = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-allowed_extensions", "ALL",
         "-i", m3u8, "-ac", "1", "-ar", str(SR), "-f", "s16le", "-y", out_pcm],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg decode: {r.stderr[-300:]}")
    return os.path.getsize(out_pcm) / 2 / SR


def flatten_units(units):
    """corpus units -> (all_words [(unit, word, off)], norm_words, char_costs)."""
    all_words, norm, costs = [], [], []
    for ui, text in enumerate(units):
        for w, off in tokenize(text):
            all_words.append((ui, w, off))
            norm.append(normalize_word(w))
            costs.append(len(w) + 1)
    return all_words, norm, costs


def align_stream(aligner, track, slug, work, workdir):
    prefix = TRACKS[track]
    stream_dir = os.path.join(workdir, "stream")
    shutil.rmtree(stream_dir, ignore_errors=True)
    r = rclone("copy", f"{BUCKET}/{prefix}/{slug}/", stream_dir, check=False)
    if r.returncode != 0 or not os.path.exists(os.path.join(stream_dir, "index.m3u8")):
        log(f"  SKIP {slug}: no stream in R2")
        return None

    pcm_path = os.path.join(workdir, "audio.pcm")
    dur = decode_stream(stream_dir, pcm_path)
    pcm = np.memmap(pcm_path, dtype=np.int16, mode="r")

    all_words, norm_words, costs = flatten_units(work["units"])
    total_chars = sum(costs)
    rate = dur / total_chars                      # sec per char, exact global
    log(f"  {slug}: {dur/3600:.2f}h, {len(all_words)} words, "
        f"rate {rate*1000:.1f}s/1000ch")

    words_out = []                                # [unit, s_ms, e_ms, score]
    audio_t, wi = 0.0, 0
    n_words = len(all_words)
    windows = fallbacks = 0
    t0 = time.time()

    while wi < n_words:
        w_audio = min(WINDOW_SEC, dur - audio_t)
        final_audio = audio_t + w_audio >= dur - 1.0
        # text window: what this much audio should hold, slightly over-provisioned
        budget = w_audio * TEXT_OVERPROVISION / rate
        wj, acc = wi, 0.0
        while wj < n_words and acc < budget:
            acc += costs[wj]; wj += 1
        if final_audio:
            wj = n_words                           # last window: all remaining text
        s0, s1 = int(audio_t * SR), int((audio_t + w_audio) * SR)
        samples = pcm[s0:s1].astype(np.float32) / 32768.0

        em, frame_times = aligner.emissions(samples)
        spans = aligner.align_em(em, frame_times, norm_words[wi:wj])
        if spans is None:
            # too much text for frames (or align error): shrink text 25% and retry
            wj = wi + max(MIN_WINDOW_KEEP, int((wj - wi) * 0.75))
            spans = aligner.align_em(em, frame_times, norm_words[wi:wj])
            fallbacks += 1
            if spans is None:
                # give up on this window: skip 60s of audio, keep text cursor
                log(f"    window @{audio_t:.0f}s failed twice; skipping 60s")
                audio_t += 60
                fallbacks += 1
                if audio_t >= dur - 1.0:
                    break              # no audio left to try against
                continue
        windows += 1
        if windows % 25 == 0:
            log(f"    ... {audio_t/3600:.2f}/{dur/3600:.2f}h, {wi}/{n_words} words")
        all_text = final_audio and wj == n_words
        keep_until = w_audio - (0 if all_text else SAFETY_SEC)
        kept = 0
        last_end = None
        for k, (s_sec, e_sec, sc) in enumerate(spans):
            if e_sec > keep_until and not all_text:
                break
            ui = all_words[wi + k][0]
            words_out.append([ui, int((audio_t + s_sec) * 1000),
                              int((audio_t + e_sec) * 1000), int(sc * 1000)])
            kept += 1
            last_end = e_sec
        if kept < MIN_WINDOW_KEEP and not final_audio:
            # pathological window (silence run / garbled): advance past it.
            # Roll back the few rows already appended — the next window
            # re-aligns the same words and would otherwise duplicate them,
            # poisoning the unit downstream (rows > tokens).
            if kept:
                del words_out[-kept:]
            log(f"    window @{audio_t:.0f}s kept {kept}; advancing {SAFETY_SEC}s")
            audio_t += SAFETY_SEC
            fallbacks += 1
            continue
        wi += kept
        if wi >= n_words:
            break
        audio_t = audio_t + last_end if last_end else audio_t + w_audio
        if final_audio and kept == 0:
            break

    dt = time.time() - t0
    scores = [w[3] for w in words_out]
    qc = {"windows": windows, "fallbacks": fallbacks,
          "aligned_words": len(words_out), "total_words": n_words,
          "coverage": round(len(words_out) / max(1, n_words), 4),
          "mean_score": round(float(np.mean(scores)) / 1000, 3) if scores else 0,
          "align_seconds": round(dt, 1), "speedup": round(dur / max(dt, 1e-9), 1)}
    log(f"  {slug}: {qc['aligned_words']}/{n_words} words "
        f"({qc['coverage']*100:.1f}%), score {qc['mean_score']}, "
        f"{qc['speedup']}x realtime")
    return {"slug": slug, "track": track, "tokenizer": TOKENIZER_V,
            "duration_ms": int(dur * 1000), "rate_spc": round(rate, 6),
            "words": words_out, "qc": qc}


def upload_result(result, track, workdir):
    prefix = TRACKS[track]
    name = f"{prefix}__{result['slug']}.json.gz"
    path = os.path.join(workdir, name)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(result, f, separators=(",", ":"))
    rclone("copy", path, f"{BUCKET}/align/_streams/")
    os.remove(path)
    return name


# ── main ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--track", choices=["narration", "verse", "both"], required=True)
    ap.add_argument("--slug")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--of", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    ap.add_argument("--workdir", default="/tmp/align_work")
    args = ap.parse_args()

    os.makedirs(args.workdir, exist_ok=True)
    aligner = Aligner(args.device)
    log(f"model ready on {args.device}")

    tracks = ["narration", "verse"] if args.track == "both" else [args.track]
    done = already_done()
    log(f"{len(done)} streams already aligned in R2")

    todo = []
    for track in tracks:
        corpus = load_corpus(track, args.workdir)
        for i, work in enumerate(corpus):     # corpus is char-sorted desc
            if args.slug and work["slug"] != args.slug:
                continue
            if i % args.of != args.shard:
                continue
            name = f"{TRACKS[track]}__{work['slug']}.json.gz"
            if name in done:
                continue
            todo.append((track, work))
    # small-first within the shard so early completions prove the pipeline
    todo.sort(key=lambda tw: tw[1]["chars"])
    if args.limit:
        todo = todo[:args.limit]
    log(f"shard {args.shard}/{args.of}: {len(todo)} streams to align")

    ok = err = 0
    for track, work in todo:
        try:
            res = align_stream(aligner, track, work["slug"], work, args.workdir)
            if res is not None:
                upload_result(res, track, args.workdir)
                ok += 1
        except torch.cuda.OutOfMemoryError:
            # two shards share the GPU; free our cache and retry once
            torch.cuda.empty_cache()
            try:
                res = align_stream(aligner, track, work["slug"], work, args.workdir)
                if res is not None:
                    upload_result(res, track, args.workdir)
                    ok += 1
            except Exception as ex:
                err += 1
                log(f"  ERROR {work['slug']} (after OOM retry): {ex}")
        except Exception as ex:
            err += 1
            log(f"  ERROR {work['slug']}: {ex}")
        finally:
            # without this, the torch caching allocator balloons to ~19 GB on
            # big works and starves the sibling shard into permanent OOM
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
    log(f"SHARD_DONE ok={ok} err={err}")


if __name__ == "__main__":
    main()
