#!/usr/bin/env python3
"""
Speakable-text layer for TTS + alignment on the Indic corpus.

THE DISPLAY TEXT IS NEVER TOUCHED. This module produces, per work, a spoken
variant of each unit plus a token map back to the display tokens, so the
read-along word timings still land on the display words. Dropped tokens
(daṇḍas, verse numbers, refs) simply carry no timing.

Transform is LINE-BY-LINE and TOKEN-BY-TOKEN:
  - tokenize each display line with align_common.tokenize (whitespace split)
  - each token maps to a spoken string ('' = dropped)
  - spoken line = surviving spoken tokens joined by single spaces
  - blank display lines survive (verse stanza gaps drive TTS prosody)

Rule flags (per work, tts_rules.json):
  strip_danda        bare '/' '//' '|' '||' tokens, and trailing '/'-'//' glued
                     to a word ("dharmaḥ//" -> "dharmaḥ")
  strip_num_markers  pure number/ref tokens: "12", "12.", "1.2.3", "[12]",
                     "(3.4)", "||12||", "12ab", "12cd" (verse-quarter refs)
  strip_leading_num  leading "N." / "N.N." numbering at line start only
  strip_trailing_num trailing bare number at line end only
  iast_fold          IAST/ISO-15919 -> pronounceable ASCII (always on for
                     Sanskrit/Kawi; see IAST_MAP)
  custom_drop        list of extra regexes; a token matching any is dropped
  custom_sub         list of [pattern, replacement] applied inside tokens

IAST mapping favors how an English reader would say the name, not scholarly
romanization: ā->aa, ī->ee, ū->oo, ṛ/r̥->ri, ṝ->ree, ḷ->l, ś/ṣ->sh, c->ch is
NOT applied (Kokoro says 'k' — 'ch' would be wrong more often than 'c'),
ñ->ny, ṅ->ng, ṭ/ḍ/ṇ->t/d/n, ṃ->m, ḥ->h (drop the visarga breath).
"""
from __future__ import annotations
import json, re, unicodedata

from align_common import tokenize

# ── IAST / ISO-15919 → speakable ascii ─────────────────────────────────────

IAST_MAP = {
    "ā": "aa", "Ā": "Aa", "ī": "ee", "Ī": "Ee", "ū": "oo", "Ū": "Oo",
    "ṛ": "ri", "Ṛ": "Ri", "ṝ": "ree", "Ṝ": "Ree", "r̥": "ri", "R̥": "Ri",
    "ḷ": "l", "Ḷ": "L", "ḹ": "lee",
    "ś": "sh", "Ś": "Sh", "ṣ": "sh", "Ṣ": "Sh",
    "ñ": "ny", "Ñ": "Ny", "ṅ": "ng", "Ṅ": "Ng",
    "ṭ": "t", "Ṭ": "T", "ḍ": "d", "Ḍ": "D", "ṇ": "n", "Ṇ": "N",
    "ṃ": "m", "Ṃ": "M", "ṁ": "m", "Ṁ": "M", "ḥ": "h", "Ḥ": "H",
    "ē": "e", "Ē": "E", "ō": "o", "Ō": "O",
    "ĕ": "e", "ŏ": "o", "ẖ": "h", "ḫ": "h",
    # Kawi schwa (NFKD would delete it: "tarəṅ" -> "tarng")
    "ə": "e", "Ə": "E",
    # Gutenberg-era circumflex long vowels (Dasgupta): â=ā etc.
    "â": "aa", "Â": "Aa", "î": "ee", "Î": "Ee", "û": "oo", "Û": "Oo",
}
# build a single regex over the map keys (multi-char keys first)
_IAST_RE = re.compile("|".join(sorted(map(re.escape, IAST_MAP), key=len, reverse=True)))


# Punctuation that must be mapped, not silently deleted: NFKD would DROP an
# intra-token em-dash and glue the words ("husband—when" -> "husbandwhen").
_PUNCT_MAP = str.maketrans({
    "—": "-", "–": "-", "‒": "-", "―": "-",
    "’": "'", "‘": "'", "“": "", "”": "", "…": "",
})


def iast_fold(word: str) -> str:
    # normalize decomposed combining forms first (r + ring-below etc.)
    w = unicodedata.normalize("NFC", word)
    w = w.translate(_PUNCT_MAP)
    w = _IAST_RE.sub(lambda m: IAST_MAP[m.group(0)], w)
    # anything still non-ascii: strip diacritics, keep base letters
    w = unicodedata.normalize("NFKD", w).encode("ascii", "ignore").decode("ascii")
    return w


# ── token-level droppers ───────────────────────────────────────────────────

_DANDA_TOKEN = re.compile(r"^[/|]{1,3}$")
_NUM_TOKEN = re.compile(
    r"^[\[\(\|/]*\d+[\.\d]*(ab|cd|abc|d)?[\]\)\|/\.:;,]*$")
_LEADING_NUM = re.compile(r"^\d+[\.\d]*[\.\):]$")
_TRAILING_DANDA = re.compile(r"[/|]{1,3}[\.,;]?$")


_MARKUP = re.compile(r"[*_`#>]+")


def spoken_token(tok: str, flags: dict, at_line_start: bool, at_line_end: bool) -> str:
    """Display token -> spoken string ('' = dropped). Never returns a string
    containing whitespace (the token map must stay 1:1 with the alignment
    tokenizer over the spoken text)."""
    # NFC first so non-ASCII drop patterns can't silently miss on a future
    # NFD re-export of the corpus
    tok = unicodedata.normalize("NFC", tok)
    # drops are checked BOTH before and after subs/markup-stripping: some
    # per-work drop patterns key on the raw form ("**PS_1**"), others only
    # match after the wrapping is gone
    for pat in flags.get("custom_drop", []):
        if re.search(pat, tok):
            return ""
    t = tok
    for pat, rep in flags.get("custom_sub", []):
        t = re.sub(pat, rep, t)
    t = _MARKUP.sub("", t)          # markdown residue is never speakable
    for pat in flags.get("custom_drop", []):
        if re.search(pat, t):
            return ""
    if flags.get("strip_danda"):
        if _DANDA_TOKEN.match(t):
            return ""
        t = _TRAILING_DANDA.sub("", t)
    if flags.get("strip_num_markers") and _NUM_TOKEN.match(t):
        return ""
    if flags.get("strip_leading_num") and at_line_start and _LEADING_NUM.match(t):
        return ""
    if flags.get("strip_trailing_num") and at_line_end and re.match(r"^\d+[\.\d]*$", t):
        return ""
    if flags.get("iast_fold"):
        t = iast_fold(t)
    # a token reduced to bare punctuation says nothing
    if not re.search(r"[A-Za-z0-9]", t):
        return ""
    return "".join(t.split())       # paranoia: a spoken token is one token


def spoken_unit(text: str, flags: dict):
    """Display unit text -> (spoken_text, map) where map[i] = display token
    index (into align_common.tokenize(text)) for the i-th SPOKEN token."""
    disp_tokens = tokenize(text)          # [(word, offset)] over the whole unit
    # line-relative bookkeeping: walk lines, but keep global token indexing
    spoken_lines, tok_map = [], []
    gi = 0
    pos = 0
    for line in text.split("\n"):
        line_toks = tokenize(line)
        out = []
        for li, (w, off) in enumerate(line_toks):
            sp = spoken_token(w, flags, li == 0, li == len(line_toks) - 1)
            if sp:
                out.append(sp)
                tok_map.append(gi)
            gi += 1
        spoken_lines.append(" ".join(out))
        pos += len(line) + 1
    assert gi == len(disp_tokens), "line tokenization must match unit tokenization"
    return "\n".join(spoken_lines), tok_map


def load_rules(path: str) -> dict:
    return json.load(open(path, encoding="utf-8"))
