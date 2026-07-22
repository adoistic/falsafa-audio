# Findings — Falsafa Audio

## Corpus format
- Work dir: `/Users/siraj/falsafa/corpus/works/<slug>/`
  - `index.md` (frontmatter: id, slug, title, author, genre, language, total_logical_chapters…)
  - `chapters/<ch-slug>/`
    - `meta.json` — has `layout` (verse|prose), `layouts_in_variants`, `default_variant`
      (`translation.md` or `original.md`), `variants[]`, chapter_number, chapter_slug.
    - `translation.md` and/or `original.md` — frontmatter + body. English text in translation.md
      for translated works; ECPA/original-English works have only `original.md`.
    - `translation.paragraphs.json` / `original.paragraphs.json` — `[{id, offset, text}]`
      where `offset` = char offset of paragraph start within the chapter body.

## Unit / dataset format (reverse-engineered)
- `dataset/<slug>.jsonl`, one JSON obj per unit:
  `{work, title, language, mode, unit, ref_start, ref_end, chars, text}`
- `mode` = "song" (verse) | "narration" (prose)
- `ref_start`/`ref_end` = `<chapter_slug>:<paragraph_offset>` (offset from paragraphs.json)
- `unit` = 0-based index within the work
- Budgets: SONG ~1,800 chars, NARR ~3,500 (README). Observed p95: song 2,313 / narr 5,668;
  means song 1,939 / narr 4,165. Max song 38k, narr 70k => giant single paragraphs are NOT
  hard-split; accumulate paragraphs until adding the next exceeds budget, then emit.
- ~15% of units span >1 chapter => consecutive same-mode chapters are concatenated then chunked.

## Root cause of the 180 gap
- Old pipeline read `translation.md` only. The 180 ECPA poetry works have only `original.md`
  (English source, no translation) => 0 chapters detected => class "unknown", no dataset file.
- They DO have chapters now: e.g. richard-berenger-poems-8c5b99 has 4 verse chapters.
- All 180 are `class: unknown`, `total_chapters: 0` in classification/works.json, genre Poetry,
  ECPA source. Verse => become song units. (Pure poetry => skipped by Phase 3 internal pass.)

## Internal-classification reality
- Corpus does NOT structurally mark verse-within-prose. Chapter `layout` is whole-chapter.
- Embedded verse is prose-reflowed (line breaks lost). Example — Diogenes Laertius ch 03
  (Life of Plato, layout=prose): epigram "Star-gazing Aster, would I were the skies, / To gaze
  upon thee with a thousand eyes" sits inline in a prose paragraph. Only textual cues mark it:
  "the following epigrams", "wrote thus upon Dion:", "runs thus:", "another on the manner of
  his death:". => LLM semantic detection required; regex only as pre-filter.
- Mixed works today: verse *chapters* -> song, prose chapters -> narration. Diogenes has
  chapter 05 tagged verse (34 song units); embedded epigrams in prose chapters are NOT captured.

## Pre-filter sizing (scan_cues.py, threshold >=2 cues)
- 20,652 prose chapters scanned (poetic works excluded).
- Candidates: 1,461 chapters across 564 works. By class: prose 1,439, mixed 18, unknown(180) 4.
- Cue regex: verses|epigram|epitaph|these lines|the following lines|runs thus|wrote thus|
  as follows|sang thus|in these words|thus:|poem|couplet|stanza|ode|hymn|elegy.

## Not the pipeline
- `scripts/chapter-splitting/` in main repo = tool to split monolithic works into chapters
  (TYPE_A..E), NOT audio verse/prose. Unrelated to this task.
- `apps/site/src/pages/listen.astro` = "coming soon" page, links to the audio repo.
