# Song-highlight curation

You are selecting the passages from a philosophical/literary corpus that will be turned
into actual songs by a music model. Everything else in the corpus gets read aloud by a
TTS narrator; this is the small curated layer that gets *sung*. So the bar is high: a
listener should hear one of these and think "that's beautiful", not "that's a database
dump set to music".

## Input

Read `highlights/batches/batch_NNN.json` — a list of works, each with metadata and a
`candidates` array of passages already extracted from the work's verse.

## Task

For **each work in the batch**, choose **1 to 3** candidate passages.

Pick for impact, not for representativeness. A good pick is:

- **Self-contained** — it means something to someone who has not read the work.
- **Striking** — a real image, a turn of thought, a line that lands. The passages people
  actually quote from this work, if any are present.
- **Singable** — rhythm, repetition, a refrain, concrete nouns. Verse that moves.

Reject: catalogues of names, genealogies, stage directions and speaker tags, translator's
prefaces and apparatus, mangled or garbled text, passages that are only half a sentence,
and anything whose sense depends on the surrounding page.

If a work's candidates are genuinely all weak, still return your **single least-bad** pick
— every work in the corpus should end up with at least one song. Only return an empty
`picks` array if the passages are unusable as text (garbled, non-lyrical fragments).

## Style prompt

For each pick, write a `style`: a comma-separated prompt for a music generation model,
matched to the work's own tradition, era and mood — *not* to generic pop. Include genre,
instrumentation, vocal character, and tempo/feel. Examples of the register wanted:

- Vedic hymn → `devotional chant, drone, tanpura, deep male voice, slow, reverent, sparse`
- Ghalib ghazal → `melancholy ghazal, harmonium, tabla, expressive male vocal, rubato, intimate`
- Greek tragedy chorus → `ancient chorus, lyre, frame drum, layered voices, solemn, building`
- Shakespeare sonnet → `renaissance art song, lute, countertenor, gentle, chamber`
- Old English verse → `nordic folk, bowed lyre, low male chant, stark, cold`
- English romantic lyric → `pastoral folk ballad, acoustic guitar, warm female vocal, unhurried`

Match the *work in front of you*. A Latin satire and a Latin hymn should not get the
same style.

## Output

Write `highlights/results/batch_NNN.json` (create the directory if needed):

```json
[
  {"slug": "<work slug exactly as given>",
   "picks": [
     {"id": "c07", "style": "...", "why": "one short line on why this passage"}
   ]}
]
```

Rules:
- `id` must be a candidate id that exists in that work's `candidates` array. Never invent
  one, never edit the passage text — selection only, the text is carried over verbatim.
- One entry per work in the batch, in the same order. Do not skip works.
- Write the file, then reply with only: `batch_NNN: <n works> <n picks>`.
