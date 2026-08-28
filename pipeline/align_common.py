#!/usr/bin/env python3
"""
Shared text tokenization for forced alignment.

The GPU worker (align_worker.py) and the local post-processor (align_merge.py)
MUST tokenize unit text identically: the worker stores one [unit, start_ms,
end_ms, score] row per token emitted here, in order, and the post-processor
re-tokenizes the same corpus text to recover the word strings and their char
offsets. Any change here invalidates every stored alignment — bump TOKENIZER_V
and re-run rather than editing in place.

Normalization targets the MMS_FA label set (lowercase a-z + apostrophe).
Digits are expanded with num2words (Kokoro spoke them as words); anything that
normalizes to nothing aligns as a <star> token so it still gets a time span.
"""
from __future__ import annotations
import re
import unicodedata

TOKENIZER_V = "v1"

_WORD_RE = re.compile(r"\S+")

try:
    from num2words import num2words
except ImportError:  # tokenize() works without it; normalize_word degrades to star
    num2words = None


def tokenize(text: str):
    """Unit text -> [(word, char_offset), ...]. Whitespace-split, offsets exact."""
    return [(m.group(0), m.start()) for m in _WORD_RE.finditer(text)]


def fold_ascii(s: str) -> str:
    """IAST/diacritics -> plain ascii (ā->a, ṛ->r, …), matching what an English
    acoustic model hears Kokoro approximate."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


_NUM_RE = re.compile(r"^\d{1,7}$")
_CLEAN_RE = re.compile(r"[^a-z']")


def normalize_word(word: str) -> str:
    """Word -> CTC label string ('' means unalignable -> use <star>).

    Pure digit runs expand to words ("1848" -> "eighteen forty-eight" is how
    Kokoro reads years; num2words gives "one thousand..." — close enough for
    alignment since CTC absorbs local mismatch). Mixed alnum keeps its alpha.
    """
    w = fold_ascii(word).lower()
    digits = re.sub(r"\D", "", w)
    alpha = _CLEAN_RE.sub("", w)
    if not alpha and digits and _NUM_RE.match(digits) and num2words is not None:
        try:
            n = int(digits)
            # Years read as pairs ("1848" = eighteen forty-eight); other numbers as cardinal
            if 1100 <= n <= 2099 and len(digits) == 4:
                spoken = num2words(n, to="year")
            else:
                spoken = num2words(n)
            return _CLEAN_RE.sub("", fold_ascii(spoken).lower().replace("-", "").replace(" ", ""))
        except Exception:
            return ""
    return alpha
