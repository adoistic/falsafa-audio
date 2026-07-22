# falsafa-audio

The audio layer of [Falsafa](https://falsafa.ai) — turning the corpus into things
you can **listen to**.

Every work in Falsafa is classified and segmented by how it should be heard:

- **Verse → songs.** Poetic passages (the Rāmāyaṇa, the Mahābhārata, the Vedas,
  Homer, Virgil, Ghālib, Iqbāl…) are chunked into song-sized units to be set to
  music with tools like [Suno](https://suno.com). Imagine the whole Rāmāyaṇa as a
  song cycle — or a single couplet as a track.
- **Prose → audiobooks.** Essays, treatises, histories and dialogues are chunked
  for text-to-speech narration.
- **Mixed works → both.** Prosimetric texts (Suetonius' *Lives* with their
  embedded epigrams, Athenaeus, Diogenes Laertius, Menippean satire) are narrated
  as prose with the verse passages breaking out as songs.

> **English first.** We start from the English translations, because compatibility
> of the original languages with music/TTS generation is unproven. Selected works
> in the original language may follow.

## What's here

```
classification/works.json   every work: language, genre, class (poetic/prose/mixed —
                            derived from actual song/narration units, not just
                            chapter layout), verse vs prose chapter counts, per-chapter layout
classification/verse-spans.json  embedded-verse paragraph offsets per work/chapter,
                            from the passage-level classification grind
dataset/index.json          per-work summary: song_units + narration_units
dataset/<slug>.jsonl        one JSON object per audio unit:
                            {work, title, language, mode, unit, ref_start, ref_end, chars, text}
                            mode = "song" (verse) | "narration" (prose)
```

Song units are capped ~1,800 chars (Suno-friendly); narration units ~3,500.
Each unit carries its source reference range (`ref_start`/`ref_end`) back into the
Falsafa corpus.

## Scale (v2, chapter- + passage-level classification)

- **2,018** works — full corpus coverage, no gaps
- **26,336** song units (verse)
- **87,641** narration units (prose)
- **321** works are `mixed` (up from 39) — embedded verse now breaks out of prose
  chapters into its own song units, not just whole verse chapters

## Pipeline

Committed under `pipeline/`: `segment.py` (paragraph-aware chunker), `build.py`
(corpus → dataset), `detect_candidates.py` + `make_tasks.py` + `plan_jobs.py` +
`plan_batches.py` + `merge_spans.py` (the passage-level classification grind —
subagents read numbered paragraphs of candidate prose chapters and identify which
are embedded verse; results are validated and merged before rebuilding).

## Roadmap

- [x] Work- and chapter-level verse/prose classification (from corpus `layout` tags)
- [x] **Passage-level (granular) classification** — verse-within-prose inside mixed
      works (epigrams, epitaphs, hymns, oracles-in-verse quoted mid-chapter) is
      detected and broken out into its own song units. 300 works gained embedded
      verse; e.g. Diogenes Laertius went from 34 to 115 song units.
- [ ] Distinct verse-block treatment in the [falsafa.ai](https://falsafa.ai) reader
- [ ] Generated audio (songs + audiobooks) via API
- [ ] Original-language audio for selected works

## Source

Derived from the Falsafa corpus ([adoistic/falsafa](https://github.com/adoistic/falsafa)).
Text is the in-house English translation layer. Public domain / research use.
