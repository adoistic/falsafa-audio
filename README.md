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
classification/works.json   every work: language, genre, class (poetic/prose/mixed),
                            verse vs prose chapter counts, per-chapter layout
dataset/index.json          per-work summary: song_units + narration_units
dataset/<slug>.jsonl        one JSON object per audio unit:
                            {work, title, language, mode, unit, ref_start, ref_end, chars, text}
                            mode = "song" (verse) | "narration" (prose)
```

Song units are capped ~1,800 chars (Suno-friendly); narration units ~3,500.
Each unit carries its source reference range (`ref_start`/`ref_end`) back into the
Falsafa corpus.

## Scale (v1, chapter-level classification)

- **1,838** works
- **14,949** song units (verse)
- **84,098** narration units (prose)

## Roadmap

- [x] Work- and chapter-level verse/prose classification (from corpus `layout` tags)
- [ ] **Passage-level (granular) classification** — split verse-within-prose inside
      mixed works, so an epigram inside a prose biography becomes its own song unit.
      Drives both this dataset *and* a distinct verse-block treatment in the
      [falsafa.ai](https://falsafa.ai) reader.
- [ ] Generated audio (songs + audiobooks) via API
- [ ] Original-language audio for selected works

## Source

Derived from the Falsafa corpus ([adoistic/falsafa](https://github.com/adoistic/falsafa)).
Text is the in-house English translation layer. Public domain / research use.
