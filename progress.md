# Progress Log — Falsafa Audio

## Session 1 (2026-07-18)
- Investigated both repos. Confirmed: 1,838/2,018 works in dataset; 180 missing (pure-verse
  ECPA, only original.md); internal verse-in-prose classification not done (chapter-level only).
- Reverse-engineered unit format + segmentation params (findings.md).
- Confirmed original generator script is absent from both repos → will reconstruct.
- Ran corpus-wide verse-cue pre-filter: 1,461 candidate chapters / 564 works for LLM pass.
- Set up planning files. Starting Phase 0 (reconstruct segmenter).

### Phase 0/1 complete
- segment.py validated (58/62 exact), body->paragraphs fallback (ECPA verse works had no
  paragraphs.json — the real reason they were skipped). build.py --missing ingested all 180.
- Now 2,018 works / 21,374 song / 85,152 narration. Every work has a non-empty jsonl.

### Phase 3 grind setup
- detect_candidates.py -> 2,070 candidate chapters / 702 works (skip poetic).
- make_tasks/render_tasks/plan_jobs -> 2,507 bounded jobs; plan_batches -> 201 batches.
- Validated subagent classification on Diogenes ch03: 17/17 exact offsets, sound judgment.
- Adnan directive: use SUBAGENTS, not OpenRouter API. (OpenRouter smoke tests only, ~cents,
  nothing persisted.) Recorded in memory.
- Dispatched first wave: batches 43,44 (Diogenes/mixed, for end-to-end verification) + 0,1,2,3.
  Awaiting completions -> then merge_spans.py + build.py + verify song units appear.

### Phase 3 grind — dispatch log
- Wave 1: batches 0,1,2,3,43,44 (verify). Wave 2: batches 4-19. ~20 batches in flight.
- Override format bug fixed (offset-set). Marker-coalescing added.
- Cursor file: classification/.dispatch-cursor (next batch to dispatch = 20).
- Loop: waiter fires when results ~drain -> dispatch next ~20 batches (idempotent) -> repeat.
- Finalize deferred to end: merge_spans.py -> build.py affected slugs -> reindex + README.

### RESUMABLE STATE (update as I go)
- 2026-07-19 06:10 IST: hit session usage limit (resets 12pm IST). ~20 in-flight subagents
  either errored with "session limit" or lost state when the harness restarted. Idempotent
  per-job writes mean MOST of that work survived — verified via jobid-level check against each
  batch file. Genuinely incomplete batches needing re-dispatch once the limit resets:
  65(0/16) 82(0/13) 83(0/9) 86(0/8) 87(0/12) 90(0/11) 94(0/10) 81(5/10) 89(7/12) 96(1/15)
  97(7/12) 99(9/20) 93(10/11) 66(11/14). Batches 100-201 never dispatched.
- Results at pause: 1129/2507 jobs done, 116/201 batches fully done.
- To resume: `python3 pipeline/plan_batches.py --pending` lists ALL unfinished batches (superset
  of the list above, safe/idempotent to just re-dispatch every pending one). Dispatch subagents
  per SUBAGENT_PROMPT.md contract (skip already-done jobids automatically). Continue cursor from
  100 for never-launched batches. After ALL 201 batches show 0 pending: merge_spans.py ->
  build.py <affected slugs> (or loop over classification/verse-spans.json keys) -> reindex.
- Big verse-heavy works seen: Prudentius/Ausonius (batch16, 1950), Persius (batch15, 663),
  Cicero De Divinatione (batch27, 317), Callimachus hymns (batch21), Aristophanes (batch10).
- FINALIZE when all done: merge_spans.py -> build.py <affected slugs> -> reindex -> commit.
- Subagent quality high (Diogenes epigrams, Mandeville Grumbling Hive, Aeschines Eion epigrams
  all correctly found; idioms/prose block-quotes correctly excluded).

## Session 2 (2026-07-19) — grind completion + finalize
- Resumed after usage-limit pauses (idempotent per-job design meant zero lost work).
- All 2,507 jobs / 201 batches complete. merge_spans.py: 300 works gained embedded
  verse, 17,345 verse paragraphs, 31 invalid offsets dropped (0.18% noise, expected).
- Bug found: works.json `class` field used raw chapter layout, so works with ONLY
  sub-chapter verse (no whole verse chapter) stayed "prose" post-rebuild — caught via
  Suetonius Tiberius/Divus Augustus regressing from mixed->prose. Fixed classify() to
  derive class from final song/narration unit counts. Rebuilt all 2,018 works.
- 5 GRETIL works (rigveda-aufrecht, valmiki-ramayana, atharvaveda-ps, samaveda,
  mahabharata) use a different `ref`-based paragraph schema (not `offset`) from a
  separate ingestion pipeline; --all rebuild skips them safely (already poetic,
  out of scope, files untouched/intact).
- Full-corpus validation: 113,977 units, 0 bad JSON/empty text/bad mode. The only
  anomaly (8,737 off-by-one `chars` field mismatches) is confined to those same 5
  untouched GRETIL files — pre-existing quirk in their separate pipeline.
- Removed classification/verse-render/ (129M) + pipeline/render_tasks.py — confirmed
  orphaned, superseded by plan_jobs.py's own chunk renderer.
- README updated: v2 stats, pipeline section, roadmap checkbox checked.
- Repo now ~914M (verse-jobs/ 133M + verse-tasks/ 136M are load-bearing pipeline
  intermediates, regenerable from candidates.json; left for user to decide re: git).
- NOT committed — awaiting explicit go-ahead per commit policy.

### Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| segmenter `o` unbound | 1 | moved `o=p['offset']` out of `if spans` |
| budget boundary drift | 1-3 | join sep is single "\n"; budget counts joined len -> exact match |
| ECPA works 0 units | 1 | no paragraphs.json → added _paragraphs_from_body fallback |
| Suetonius mixed->prose regression | 1 | classify() used chapter layout not unit modes; fixed + rebuilt all |
| build.py --all KeyError 'offset' | 1 | 5 GRETIL works use ref-based paragraphs (different pipeline); confirmed out of scope, untouched, safe |

## Session 3 (2026-07-23) — audio production: narration, verse, song curation

### Standing instruction from Adnan
Take narration and the verse track to completion, then **tear the GPU fleet down** — it
bills by the hour and nothing after this leg needs it. **Stop at the music boundary**:
when the corpus is fully narrated and the song selections are ready to generate, do NOT
start ACE-Step work. Write a handoff prompt for a fresh session instead. ACE-Step is a
separate leg of work with its own pods, its own budget, and its own session.

### Decisions
- Full-corpus song generation is OFF. Every open song model measures 35-51% lyric WER on
  contamination-free benchmarks vs our narration's measured 3.2%. Verse gets a faithful
  spoken reading; songs are a curated highlight layer only.
- Verse voice: **bm_george**, 0.92x, chosen by ear over af_heart/bm_fable/am_michael/
  am_fenrir/bf_emma/af_bella ("and it's not even close").
- Song model, if/when it runs: **ACE-Step 1.5 XL** (MIT, ungated). Only open model that
  sings lyrics, is commercially usable, is actively maintained, and has real standing on
  a blind human-preference leaderboard (Music Arena vocal #6, top open weight).

### Built
- `pipeline/audition.py` — verse voice audition; verse prosody (line rests, slower rate).
- `pipeline/make_corpus.py` — builds narration_corpus / verse_corpus for the fleet.
- `pipeline/highlights.py` — candidates / batches / merge for the song-highlight grind.
- `pipeline/HIGHLIGHTS_PROMPT.md` — the curation contract for subagents.
- `pipeline/song_worker.py` — ACE-Step 1.5 XL worker. **Written, never executed.** Its
  API follows upstream docs/en/INFERENCE.md; `--probe` prints the installed surface.
- `pod_worker.py` gained `--track {narration,verse}`: verse synthesises line by line
  with 0.32s line rests / 0.6s stanza rests, uploads to `verse/<slug>/`.
- `fleet_ssh.py` gained `--relaunch --track ...`: re-scps the worker and starts a new
  track on already-provisioned pods, no re-setup.

### Numbers
- narration: 1,750 works, 87,641 units, 353.3M chars, ~6,024 audio-hours
- verse:       589 works, 26,336 units,  47.0M chars, ~801 audio-hours (~10 GPU-hours)
- song highlights: 563 works had singable verse -> 8,056 candidate passages -> 70 subagent
  batches -> **1,261 selections across 552 works**, 0 invalid refs, 78 trimmed to the
  1,200-char cap, 22 flagged `thin`, 11 works rejected as apparatus-only.

### Verification
- Duration-ratio + level check on 100% of works, on the pod, before upload.
- Verse needed its own expectation: duration depends on line counts, not just chars, and
  the rate is voice-specific (bm_george 83.4 s/1000 chars vs af_heart 69.4 at the same
  speed setting). Leaving the narration constant in place would have rejected good audio.
- End-to-end check from R2: pulled Philostratus *Nero* back out of the bucket, 47
  segments, AAC 24kHz mono, decoded a slice from 5:00 cleanly.

### Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| 187 works yielded 0 highlight candidates | 1 | window required >=6 lines; verse embedded in prose arrives as one wrapped paragraph. Dropped the line-count criterion, added a longest-scrap fallback -> 563 works |
| verse duration ratio 1.69, near the reject band | 1-2 | expectation ignored line rests and the voice's own pace; made it gap-aware and voice-calibrated |
| glossary markup `{{term:gloss}}` and `/* */` would be sung aloud | 1 | `clean_lyrics()` at merge; also ascii-folds diacritics since ACE-Step romanizes stochastically |
| one selection 6,824 chars | 1 | `trim()` cuts at a line boundary to a 1,200-char cap |

### OPEN ISSUE — orphaned shards (found 2026-07-23, mid-run)
The fleet runs `--of 32` (a 16-pod plan) but only 12 pods x 2 procs = **shards 0-23**
were ever launched. `pod_worker` claims work with `i % of == shard`, so the 432 works
assigned to shards 24-31 are claimed by nobody. Net of what the old `--of 64` calibration
pod happens to cover: **405 works / ~1,287 audio-hours / 21.4% of the corpus**.

This fails silently and invisibly: every live shard reaches SHARD_DONE with zero errors,
and the run looks complete at 79% coverage.

**Fix (do not disturb the running fleet):** when the live shards drain, relaunch with
`--of 24`. `already_done()` re-lists R2 at startup, so each shard skips finished works
and the orphans redistribute evenly. Total GPU-work is unchanged either way; restarting
now would only throw away in-flight partial works.

**Guard worth adding before the next fleet run:** have the worker refuse to start when
`--of` exceeds the number of shards actually launched, or have the fleet derive
`shards_total` from the pods it really brought up rather than from the requested count.

### Rejects seen during the run (all understood, only one recovered)
- `aurelius-prudentius-clem-apotheosis` — 26 chars ("Hymn on the Trinity" + one heading)
  on a pure-verse work. Not a work; no narration owed. Ignore.
- `seneca-hercules-oetaeus` — 154 units x ~8 chars, all cast lists / speaker tags on a
  verse tragedy. 3.25x = Kokoro drawing out a spelled cast list. Correct reject.
- `unknown-viramitrodaya` — 503k-char Sanskrit Dharmasastra translation, rejected at
  ratio EXACTLY 1.40. Reads at 86 s/1000 chars (IAST diacritics + *italic* markers),
  not the English 61.4. False negative worth ~12 audio-hours. Recovered by widening the
  narration ceiling to 1.75 (below). Rejected works were never uploaded, so already_done
  leaves them eligible; the orphan relaunch re-attempts them for free.
- Narration `tol` widened 1.40 -> 1.75. Still catches real runaway loops (2-10x).

### Narration first pass complete (2026-07-23) — R2 diff, not shard math
Ground truth from diffing R2 works/ against the 1,750-work narration corpus:
- **1,223 works done (~4,300h, 71%)**, 527 works remaining (~1,726h, 30%).
- The orphan gap is **30%, not the 21% first estimated** (under-counted the un-launched
  residue classes: shards 2,3 AND 24-31 = 10 of 32 classes ~= 31%). R2 diff is authoritative.
- Orphans include heavy works: Josephus Jewish Antiquities (44h), Mackintosh (57h),
  Marx Capital, Ricardo, Pausanias, Say, the Levellers. Not filler.
- All 23 launched shards emitted SHARD_DONE cleanly (each ~54-55 works, mostly bad=0);
  the deficit is entirely un-launched shards, not failures.

### Cost action
- Killed 10 idle pods the moment their shards finished ($9/h -> $2/h), then the last 3
  ($2/h -> $0/h) once all launched shards drained. Every output was already in R2.

### Recovery in progress
- Fresh uniform 12-pod fleet, narration --of 24 (all 24 shards launched -> full coverage,
  the original bug fixed by construction). already_done() skips the 1,223 in R2; the 527
  remaining + Viramitrodaya (widened tol) get made. Then --relaunch --track verse, verify,
  teardown, handoff. Corpus todo list snapshot: /tmp/narration_todo.json (527 slugs).

### Recovery tail — load imbalance (not a correctness issue)
The --of 24 recovery inherited UNEVEN residue-class loads: already-done works (from the
old --of 32 first pass) aren't uniform across the new modulus, so some --of 24 shards
had 9 works, others 37-38. Coverage is complete (every work gets made); the tail is just
slow because only 6 of 24 shards (on 3 pods) still had work while 18 sat idle. Killed the
9 fully-idle pods, kept ssh-2/10/18 to finish. A work-stealing worker (claim ANY undone
work via an atomic R2 marker, not a fixed i%of residue class) would remove this and the
launch-order fat-tail imbalance both. Worth doing before the next big fleet run.

### NARRATION COMPLETE (2026-07-23)
1,741 / 1,750 works in R2. 0 real works missing. The 9 not-made are all confirmed
verse-apparatus scraps (<=1200ch: Prudentius Apotheosis 26ch, Bion/Moschus/Statius
fragments, Seneca Hercules-Oetaeus cast list) — no prose to narrate, correct rejects.
Their content is on the verse track.

### Launch-hygiene lesson (cost a detour)
Duplicate workers for the SAME shard on the SAME pod (from re-running a launch that had
half-failed) race on a shared --workdir: one rmtrees the outdir while the other encodes
-> "Broken pipe" + rclone "directory not found". Corrupted ~4 real works (Keynes,
Isocrates, Ausonius, Livy); fixed with one clean single-worker pass (pkill + rm -rf
workdir first). RULE: never blanket-relaunch to fix a partial launch — verify coverage,
then launch ONLY the missing shards. The parallel threaded launch + --verify is the
clean pattern.

### VERSE STARTED (2026-07-23)
Fresh 12-pod fleet, verse --of 24, voice bm_george, all 24 shards verified live
(MISSING: none), workers confirmed producing. Work-count balanced (~25/shard) but
audio-hours skewed: shard 0 = 198h (holds the largest poetic works), others 22-60h,
total ~800h. Fat tail on shard 0 (~2-3h). Uploads to verse/<slug>/.
Narration monitor stopped; verse monitor on r2 verse/ count.

### Verse finish + two new deferred legs (2026-07-23, decided with Adnan)
- Mahābhārata (10.7M chars, ~246h as a single file) was generating on ssh-0 and would
  block ~4-5h to produce an unusable 246h blob. KILLED. Excluded from verse_corpus via
  make_corpus EXCLUDE={"mahabharata"} (rebuilt: 588 works, ~619h). Deferred to the
  chapter-split leg below.
- Finishing verse now: 28 small remaining works (~21h, biggest 5.2h) on a 4-pod fleet,
  then teardown.

### DEFERRED LEG A — epic chapter-splitting
7 verse works are too big to be single files: mahabharata 246h, valmiki-ramayana 59h,
deipnosophistae 55h, rigveda 40h, assorted-18thc-poetry 36h, atharvaveda 29h, ovid 21h.
6 already exist in R2 as single (seekable) files; only mahabharata isn't generated.
Plan: split each into book/canto units. The 6 done ones can be SLICED from their existing
HLS using boundaries from the alignment pass (no re-gen). Mahābhārata must be GENERATED
chapter-split from the start (and we can capture Kokoro word timings during that gen).

### DEFERRED LEG B — word-level read-along alignment (Adnan wants this)
Goal: per-word [start,end] timestamps so a reader UI highlights each word as spoken.
- ENGLISH ONLY. All TTS was Kokoro English; align the English translation text to the
  English audio. Do NOT involve transliteration/original-language.
- Method: FORCED ALIGNMENT (wav2vec2 CTC, e.g. WhisperX align stage / ctc-forced-aligner)
  on the existing R2 audio. NEVER ASR-transcribe — text is ground truth, only timing is
  solved, so mistranscription is a non-issue.
- Accuracy target: <100ms (Adnan: good enough). Clean synthetic English audio -> expect
  ~20-50ms word-boundary error. Confident under 100ms.
- Granularity: word-level (line-level falls out by grouping words). "Line" = verse newline
  lines for poetry, SENTENCES for prose (NOT whole paragraphs). Keyed to existing unit/
  offset structure. Sidecar per work (JSON/VTT) loaded next to the HLS.
- Cost: ~$30-40, one GPU pass, separate fleet, runs on audio already in R2. Independent of
  current teardown.

### COMPLETE (2026-07-23) — narration + verse + Mahabharata split done, fleet down
- Narration: 1,741 works ~6,026h. Verse: 603 works ~1,083h incl. 18/18 Mahabharata parvas.
- TOTAL: 2,344 works, ~7,109 audio-hours. RunPod spend $79.54 of $200. Fleet = 0 pods ($0/hr).
- Verse mop-up + Mahabharata-split ran as one pass on 3-4 pods (one pod unreachable-SSH,
  killed, its shards redistributed onto healthy pods; --verify confirmed MISSING:none).
- mahabharata-book-01-adi spot-check: valid HLS VOD, 8,874 segs ~24.6h (≈ expected 23.6h).
- HANDOFF.md written: audio storage, HLS streaming, generation algorithm, and the
  word-level forced-alignment read-along method (the primary deliverable per Adnan).
  Music/ACE-Step is a deferred appendix.
- REMAINING (deferred legs, documented in HANDOFF.md): (A) word-level alignment pass
  (~$30-40); (B) slice the 6 other oversized epics to book level from alignment boundaries;
  (C) music generation. Nothing running; safe to leave.

---

## 2026-07-23 — UI leg + forced-alignment fleet (three-mode reader)

Goal (session hook): Read / Listen / Read-along modes on every aligned chapter,
Spotify-grade player in the site's editorial language, lock-screen playback,
pitch-preserving speed, 2-3-word read-along highlight, verse-in-prose styling.

Shipped so far:
- `/audio/*` LIVE on falsafa.ai: falsafaai Worker serves bucket falsafa-audio
  (m3u8/m4s content types, CORS *, Range, immutable cache, Cache-API edge
  caching — R2 binding reads bypass the edge cache otherwise). Deployed via
  direct Workers API (wrangler OAuth is on the wrong account; the env's
  CLOUDFLARE_API_TOKEN lacks R2 mgmt read, which wrangler needs to validate
  bucket bindings).
- pipeline/align_worker.py — streaming windowed forced alignment (MMS_FA +
  torchaudio forced_align), per-chunk frame→time mapping, batched emissions.
  GOTCHA: torchaudio's with_star=True hardcodes batch=1 (star_dim zeros((1,T,1)))
  → use with_star=False + append the zeros column manually.
  Validated <100 ms by greedy-decode span checks (12/12, 8/8, 14/14 multi-window).
- pipeline/align_merge.py — sidecars align/<work>/book.json + ch/<ch>.json
  (blocks→lines→word [s,e,off,len]), audio-manifest.json + verse-blocks.json
  into apps/site/src/data/. ECPA works need the segment.py markdown-paragraph
  fallback (ported). Mixed works merge only when ALL streams aligned.
- Site UI (apps/site/src/audio/*): dual-deck engine (native HLS on Safari,
  hls.js/light lazy elsewhere), span playlists for mixed voices, busy-lock
  against timeupdate re-entrancy (double-advance bug found live in Frogs),
  Media Session, rate 0.75–2×, localStorage resume, bottom bar (consent-banner
  material), read-along overlay (three ink layers + --highlight chunk wash,
  rAF auto-scroll — element scrollTo smooth silently no-ops in Chromium with
  html{scroll-behavior:smooth}), verse-inline read-mode styling, /listen page.
  CSS gotcha: own display:flex rules beat [hidden] — needs explicit overrides.
- Fleet: 8 pods × 2 shards align --track both; idempotent via align/_streams
  listing. Two pods delivered broken CUDA (torch cuda.is_available False) and
  one no-SSH — killed + replaced; --verify shows 16/16.

Verified in browser (dev, localhost:4321 + prod streams): mode pills, listen
start/resume, dual-deck voice swaps (Frogs, 59 spans), chapter auto-advance
(Epictetus 1→2 with bar retitle), read-along highlight sync at 1×/1.5×,
light/dark/sepia, desktop/548px/mobile-375px.
