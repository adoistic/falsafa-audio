# Falsafa Audio — Technical Handoff

This document explains **how the audio is stored, how to stream it, how it was generated,
and exactly how to build the word-level read-along mapping**. It is written so a fresh
session or a developer can pick up the work without this conversation. Final corpus counts
are in the last section.

The music/song-generation leg (ACE-Step) is **deferred** and summarized in an appendix at
the end; it is not the focus of this handoff.

---

## 1. Where the audio lives (R2 layout)

Everything is in the Cloudflare R2 bucket **`falsafa-audio`** (distinct from the site
bucket `falsafaai` — do not sync the two; the site deploy's `rclone sync` has no excludes
and would delete audio). Access is via the S3 data plane with rclone; credentials are in
`~/.config/falsafa-deploy.env` (`R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
`R2_S3_ENDPOINT`). Set up an rclone remote as:

```
RCLONE_CONFIG_R2_TYPE=s3
RCLONE_CONFIG_R2_PROVIDER=Cloudflare
RCLONE_CONFIG_R2_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID
RCLONE_CONFIG_R2_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY
RCLONE_CONFIG_R2_ENDPOINT=$R2_S3_ENDPOINT
```

Layout:

```
falsafa-audio/
  works/<slug>/                     # NARRATION (prose). One folder per work.
    index.m3u8                      #   HLS VOD playlist
    init.mp4                        #   fMP4 init segment (shared by all segments)
    seg_00000.m4s ...               #   CMAF/fMP4 media segments, 10s each
  verse/<slug>/                     # VERSE (poetry). Same structure.
    index.m3u8, init.mp4, seg_*.m4s
    meta.json                       #   (song track only — see appendix)
  verse/mahabharata-book-NN-<parva>/# the Mahabharata split into 18 parvas (books)
  _bootstrap/                       # corpora the GPU workers pull at boot
    narration_corpus.json.gz
    verse_corpus.json.gz
  _logs/<pod-hostname>/*.log        # per-shard worker logs, shipped every 30s
```

Each `<slug>` matches the dataset file `dataset/<slug>.jsonl` in the repo, so audio and
source text are joined by slug. **Idempotency and completeness are checked by diffing the
R2 folder list against the corpus** — never trust log counts alone:

```
rclone lsf r2:falsafa-audio/works/ --dirs-only --s3-no-check-bucket   # done narration
rclone lsf r2:falsafa-audio/verse/ --dirs-only --s3-no-check-bucket   # done verse
```

---

## 2. Audio format and how to stream it

**Format: HLS (HTTP Live Streaming), VOD profile, fMP4/CMAF segments.**

- Codec **AAC-LC**, **32 kbps**, **mono**, **24 kHz** (speech; ~16 MB/hour).
- Segments are **10 s**, fMP4 (`.m4s`) sharing one `init.mp4`. Playlist is `index.m3u8`
  with `#EXT-X-PLAYLIST-TYPE:VOD` and `#EXT-X-ENDLIST` (fully seekable, not live).
- Why HLS and not a single MP3: bounded memory at generation time (an 80-hour book is
  never held in RAM), instant seeking without downloading the whole file, standard CDN
  caching, and trivial adaptive delivery later.

**To play it:** any HLS player. In the browser, Safari plays `index.m3u8` natively; other
browsers use **hls.js** (`new Hls().loadSource(url)`). The URL is
`https://<r2-public-or-worker-domain>/verse/<slug>/index.m3u8`. Seeking to any timestamp
works because it is VOD with an endlist — the player fetches only the segments it needs.

**Serving:** R2 is fronted by the `falsafaai` Cloudflare Worker (see repo `DEPLOY.md`).
Serve the `.m3u8`/`.m4s`/`.mp4` with correct content types
(`application/vnd.apple.mpegurl` for the playlist, `video/mp4`/`video/iso.segment` for
segments) and permissive `Range` support. R2 egress is $0, so streaming is free.

---

## 3. How the audio was generated (the algorithm)

Pipeline code is in `pipeline/`. The worker is `pod_worker.py`; the corpus builder is
`make_corpus.py`; fleet orchestration is `fleet_ssh.py`.

**TTS model:** [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) (StyleTTS2-family),
run via the `kokoro` PyTorch package on CUDA. Voices: **`af_heart`** for narration,
**`bm_george`** for verse (chosen by ear). Verse also runs at **0.92× speed**.

**Per-work streaming encode (the core trick):** for each work we open **one long-running
ffmpeg** reading raw `f32le` PCM (24 kHz mono) on stdin and emitting HLS directly:

```
ffmpeg -f f32le -ar 24000 -ac 1 -i pipe:0 \
       -c:a aac -b:a 32k -ac 1 -ar 24000 \
       -f hls -hls_time 10 -hls_playlist_type vod \
       -hls_segment_type fmp4 -hls_fmp4_init_filename init.mp4 \
       -hls_segment_filename seg_%05d.m4s  index.m3u8
```

Text units (paragraphs for prose, stanzas for verse) are synthesized one at a time and
their PCM piped into that ffmpeg. Memory stays bounded regardless of work length. **Verse
prosody:** verse is synthesized **line by line** (split on `\n`) with a **0.32 s rest**
after each line and **0.6 s** at blank-line stanza breaks — without this Kokoro collapses
newlines and a sonnet becomes one run-on sentence.

**Verification before upload (100% of works):** duration-ratio (actual vs expected from
char count × rate) within a tolerance band, plus a `volumedetect` mean-level check to catch
silent/looping output. Narration rate ≈ 61.4 s/1000 chars, tolerance (0.70, 1.75); verse
(bm_george) ≈ 83 s/1000 chars, tolerance (0.60, 1.45). **KNOWN FLAW:** `verify()` decodes
the *entire* file for the level check — trivial for a 20-min work, but ~1 hour for a
177-hour file. Cap the decode window (e.g. probe only the first/last N minutes, or skip
`volumedetect` above some length and rely on duration alone) before running any huge work.

**Fleet model:** `fleet_ssh.py` provisions plain RunPod pods (RTX 4090/3090), waits for
SSH, installs deps, and launches N shards. Sharding is `index % of == shard` over the
char-sorted corpus; **`already_done()` lists R2 at startup and skips finished works**, so
the fleet is fully idempotent — pods can be added, killed, or preempted with no duplicate
work. Two hard-won rules:
- **Launch is fire-and-forget** (`ssh -n` + `setsid`); a launch that "times out" usually
  still started the worker. Never blanket-relaunch to fix a partial launch — that spawns
  *duplicate workers on the same shard/workdir* which race and corrupt output
  (Broken pipe / directory-not-found). Instead **`fleet_ssh.py --verify`** reports which
  `--shard N` processes are actually running; launch only the missing ones.
- **Coverage is verified, never assumed.** `--verify` must show `MISSING: none`. A
  pod-count/shard-count mismatch silently orphans residue classes (this cost a 30% gap
  once, caught only by the R2 diff).

**Imbalance note:** char-sorted round-robin puts similar-length works in the same residue
class, so a few shards get the giants and lag. For a *fresh* corpus this is mild; for a
*recovery* pass (idempotent skip makes leftovers uneven mod N) it is severe. A future
improvement is a work-stealing worker (claim any undone work via an atomic R2 marker
instead of a fixed residue class).

---

## 4. Word-level read-along mapping — how to build it

**Goal:** per-word `[start_ms, end_ms]` timestamps so a reader UI highlights each word as
it is spoken (karaoke-style), with line- and paragraph-level grouping derived for free.

**Scope decision (Adnan):** English only. All TTS is Kokoro English; align the **English
translation text** (what we spoke) to the **English audio**. Do **not** involve the
transliterated or original-language source. Target accuracy **< 100 ms** (good enough).

**Method: FORCED ALIGNMENT — never ASR transcription.**
Transcribing (Whisper etc.) then matching back is the wrong tool: it re-derives the words
and inherits mistranscriptions. Forced alignment instead takes the **known text as ground
truth** and only solves *timing* — so mistranscription cannot happen. On clean synthetic
English audio, word-boundary error is typically ~20–50 ms, comfortably under 100 ms.

**Recommended stack:**
- `ctc-forced-aligner` or **WhisperX's alignment stage** (wav2vec2 CTC acoustic model).
  Both take (audio, known transcript) → word-level timestamps. Run on GPU (~100–200×
  realtime; the whole ~7,000 h corpus is ~35–50 GPU-hours ≈ $30–40, one pod fleet).
- Input audio: decode each work's HLS to a wav/PCM stream (ffmpeg), or align per-unit.
- Input text: the ordered `units` from `dataset/<slug>.jsonl` (already the exact text fed
  to TTS). Concatenate in unit order; the aligner places every word across the stream.

**Granularity and the definition of a "line":**
- Get **word-level** first; **line-level and paragraph-level fall out by grouping** (a
  line's start = its first word's start, end = its last word's end). Do not build them
  separately.
- A "line" is **not a paragraph**. For **verse**, a line is the `\n`-separated poetic line
  (the same split used for prosody). For **prose**, split each paragraph into **sentences**
  — each sentence is a highlightable line.

**Output format:** one sidecar per work, loaded next to the HLS stream, e.g.
`verse/<slug>/align.json`:

```json
{
  "slug": "...",
  "words": [{"w": "From", "s": 1234, "e": 1401, "unit": 0, "line": 0}, ...],
  "lines": [{"unit": 0, "line": 0, "s": 1234, "e": 4880, "text": "From fairest..."}],
  "units": [{"unit": 0, "s": 1234, "e": 61050}]
}
```

Times in ms from the start of the work's audio. Keyed to the `unit` index (and, for verse,
the line index within the unit) that already exist in the dataset — so the player maps a
word directly to a position in the source text.

**For the split epics** (Mahabharata books, and if desired the other 6 oversized works):
align **per book/parva** — each `verse/mahabharata-book-NN-*/` folder gets its own sidecar.
This is also where the epic-splitting and alignment converge: the 6 epics that already
exist as single files can be **sliced into book-level folders using the unit boundaries the
alignment produces**, with no re-encoding.

---

## 5. Corpus state (fill at teardown)

- Narration (prose): **1,741 works** in `works/`, ~6,026 audio-hours. 9 not made are
  apparatus-only scraps (headings/cast-lists with no prose) — correct.
- Verse (poetry): **603 works** in `verse/`, ~1,083 audio-hours, incl. **18/18 Mahabharata
  parvas** as `verse/mahabharata-book-NN-*`. The monolithic 246h Mahabharata was
  deliberately NOT generated as one file (see `pipeline/split_epics.py`); 4 not-made verse
  works are apparatus scraps (correct rejects).
- Six other oversized verse works exist as single (seekable) files and can be sliced to
  book/canto level from alignment boundaries when convenient: valmiki-ramayana 59h,
  deipnosophistae 55h, rigveda 40h, assorted-18thc-poetry 36h, atharvaveda 29h, ovid 21h.
- **Total: 2,344 works, ~7,109 audio-hours.** RunPod spend: **$79.54** (of $200; ~$120
  left). Fleet fully torn down ($0/hr). All audio verified on upload (duration-ratio +
  level) and by R2-diff-vs-corpus completeness.

---

## Appendix — deferred: music/song layer (ACE-Step)

A curated "creative song" layer was scoped and curated but **not generated** (separate
budget/session by instruction). State:
- **Model:** ACE-Step 1.5 XL (MIT, ungated) — the only open song model that sings lyrics,
  is commercially usable, actively maintained, and has real standing on a blind
  human-preference leaderboard. All open song models run 35–51% lyric WER on
  contamination-free benchmarks vs our narration's ~3.2%, which is why full-corpus singing
  was rejected in favor of a curated highlight layer.
- **Selections:** `highlights/selections.json` — 1,261 curated passages across 552 works,
  each with a period-matched style prompt, ASCII-folded lyrics, trimmed to a 1,200-char
  cap. Staged at `r2:falsafa-audio/_bootstrap/song_selections.json`.
- **Worker:** `pipeline/song_worker.py` — written, **never executed**. Its ACE-Step API
  usage follows upstream `docs/en/INFERENCE.md`; `--probe` prints the installed API surface
  to catch version drift before a run. Uploads to `songs/<slug>/<cand>/`.
