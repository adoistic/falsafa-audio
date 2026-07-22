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
