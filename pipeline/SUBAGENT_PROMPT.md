# Phase-3 verse-classification subagent contract

Each subagent is given ONE batch number N and processes every job in
`classification/verse-batches/batch-NNN.json` (`{"batch": N, "jobids": [...]}`).

For EACH jobid (process independently, one at a time):
1. If `classification/verse-spans/<jobid>.json` already exists → SKIP (idempotent).
2. Read `classification/verse-jobs/<jobid>.txt`: a WORK header, a CHAPTER line, then paragraphs
   each labeled `[offset] text` (one block per paragraph).
3. Identify paragraphs that are EMBEDDED VERSE — quoted poetry meant to be **sung** not narrated:
   poems, epigrams, epitaphs, hymns, oracles-in-verse, quoted lines from poets/dramatists, songs.
   The text is prose-reflowed (line breaks stripped) so verse looks like prose — judge by content
   and poetic diction.
   - Verse if predominantly quoted poetry, even with a short prose lead-in ("and he wrote thus:").
   - Prose that merely mentions/discusses poetry WITHOUT quoting verse is NOT verse.
   - Ordinary narration, lists, letters, expository prose are NOT verse.
   - When genuinely unsure → prose (conservative), but never miss clear poems/epigrams/epitaphs.
4. Write `classification/verse-spans/<jobid>.json` = `{"verse_offsets": [<the [offset] ints>]}`
   using the exact offset integers from the labels. No verse → `{"verse_offsets": []}`.

All paths are under `/Users/siraj/falsafa-audio/`. Finish every jobid, then reply with one line:
jobs written, jobs skipped, total verse paragraphs found.
