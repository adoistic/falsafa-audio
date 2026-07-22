# Task Plan — Falsafa Audio: complete coverage + internal classification

## Goal
1. **Get the 180 missing sources** into the audio dataset (they now have chapters in the
   corpus but were skipped because they only have `original.md`, no `translation.md`).
2. **Run internal (passage-level) verse/prose classification** on every non-pure-poetry
   work: detect verse embedded *inside* prose chapters and break it out into song units.
   **Skip pure-poetry works** (incl. long pure-poetry) — they are already all-song.

## Key facts (see findings.md for detail)
- Corpus: `/Users/siraj/falsafa/corpus/works/<slug>/{index.md, chapters/<ch>/{meta.json,
  translation.md|original.md, *.paragraphs.json}}`. 2,018 works.
- Audio repo: `/Users/siraj/falsafa-audio/` — `classification/works.json`,
  `dataset/index.json`, `dataset/<slug>.jsonl`. 1,838 works currently.
- Original generator script is **not committed** — reconstruct it (format below).
- Unit format: `{work,title,language,mode,unit,ref_start,ref_end,chars,text}`;
  `mode`=song|narration; ref=`<chapter_slug>:<paragraph_offset>`. Budgets ~1,800 song /
  ~3,500 narration; paragraph-aware; units may span consecutive same-mode chapters.
- Current routing is **chapter-level only** (by `meta.json.layout`). Verse embedded in a
  prose chapter is NOT broken out (e.g. Diogenes Laertius epigrams are prose-reflowed —
  no formatting signal; only textual cues like "wrote thus:", "the following epigram").
- 180 missing works are all ECPA pure-verse English originals (default_variant=original.md).
- Pre-filter (>=2 verse cues) => **1,461 candidate prose chapters across 564 works** for the
  LLM pass. 20,652 prose chapters total; 81 poetic works skipped.

## Phases
- [x] **Phase 0 — Reconstruct + validate segmenter.** `pipeline/segment.py`. Validated: 62-work
      sample -> 58 exact / 4 >=90% / 0 below 90% / 0 count mismatches. Separator = single newline;
      budget counts joined length. Body->paragraphs fallback added (offsets match paragraphs.json).
- [x] **Phase 1 — Ingest the 180.** `pipeline/build.py --missing`. 177 verse->song, 3 prose
      (Wealth of Nations, Mill, Common Sense)->narration. Now 2,018 works, 21,374 song,
      85,152 narration. Every manifest work has a non-empty jsonl; only index.json changed.
- [ ] **Phase 2 — Candidate detection.** Formalize embedded-verse pre-filter (>=2 cues) ->
      1,461 candidate prose chapters / 564 works. Commit as pipeline/detect_candidates.py.
- [ ] **Phase 3 — LLM internal classification.** PARAGRAPH-granularity: feed numbered paragraphs
      of each candidate chapter to a subagent; it returns which paragraph offsets are verse.
      Idempotent per-work checkpoints in classification/verse-spans/. Validate on Diogenes/
      Athenaeus FIRST, then scale via batched subagents. Skip pure poetry.
- [ ] **Phase 4 — Re-segment affected works.** Merge -> classification/verse-spans.json;
      `build.py <slug>...` re-emits with verse paragraphs -> song units.
- [ ] **Phase 5 — Finalize.** reindex + README; verify totals; update roadmap; commit.

## Decisions
- Reconstruct rather than hunt further for the lost script (confirmed absent from both repos).
- Commit the pipeline this time (reproducibility; matches project norms).
- Pre-filter before LLM to keep the grind tractable (1.4k chapters, not 20k).
- Byte-parity with the old dataset is NOT required; identical *format* + close behavior is.

## Design decision (Phase 3)
Override granularity = PARAGRAPH (a corpus paragraph is wholly song or narration). Embedded
verse is prose-reflowed but usually sits in its own blank-line-separated paragraph (verified in
Diogenes). Subagent returns verse paragraph offsets; segmenter override flips those to song.

### MECHANISM (Adnan directive, 2026-07-18): Claude SUBAGENTS ONLY.
Do NOT use the OpenRouter API for classification. Use the Agent tool (Claude subagents).
The subagent approach was validated on Diogenes ch03: 17/17 exact offsets, sound judgment.
Grind = batches of job files dispatched to subagents; each subagent reads verse-jobs/<jobid>.txt,
writes verse-spans/<jobid>.json = {"verse_offsets":[...]}. Idempotent (skip existing results).

## STATUS: ALL PHASES COMPLETE (2026-07-19)
- Phase 3 grind: 2,507/2,507 jobs, 201/201 batches. 300 works gained embedded verse
  (17,345 verse paragraphs, 31 invalid offsets safely dropped by merge_spans.py).
- Phase 4: rebuilt all 2,013/2,018 works (5 GRETIL works skip cleanly — different
  ref-based paragraph schema, already poetic/out-of-scope, untouched/intact).
- Bug found+fixed: classify() used raw chapter layout for the work `class` field,
  so works with ONLY sub-chapter embedded verse (no whole verse chapter) stayed
  mislabeled "prose" (e.g. Suetonius). Fixed to derive class from final song/
  narration unit counts. mixed: 39 -> 321.
- Final: 2,018 works, 26,336 song units, 87,641 narration units. Full-corpus
  validation: 113,977 units, 0 bad JSON/empty text/bad mode. 8,737 off-by-one
  `chars` mismatches confined to the 5 untouched GRETIL works (pre-existing,
  unrelated pipeline quirk).
- README updated (v2 stats, pipeline section, roadmap checkbox).
- NOT YET DONE: commit. Awaiting explicit go-ahead per commit policy.

## Status: Phase 2 done; Phase 3 grind running (pipeline verified end-to-end)
- Fixed override format: verse-spans = {slug:{chapter:[offsets]}}; segmenter override = offset set.
- Added marker-coalescing (bare "**68**"/"1."/"—" paragraphs inherit running mode) so verse-heavy
  chapters don't fragment into tiny units. Paramarthasara: 20 stanzas -> 3 clean song units.
- Verified: subagent classify -> merge_spans (validates offsets) -> build.py (overrides) works.
- Dispatch loop: `plan_batches.py --pending` -> dispatch pending batches to subagents ->
  `merge_spans.py` -> `build.py <affected slugs>` -> reindex. 201 batches total.
- Wave 1 done: batches 0,1,2 (+43/44/3 finishing). Now dispatching the rest in waves.
