"""
Name normalisation for entity matching.

Matching is exact after normalising case, punctuation, whitespace and Arabic
letter variants. No words are stripped (no "Properties", "Group", "LLC", ...):
variants are listed as explicit aliases instead.
"""
import re
import unicodedata

# Arabic short vowels / marks (U+064B..U+0652), superscript alef (U+0670) and tatweel (U+0640)
_ARABIC_MARKS = re.compile(r"[ً-ْٰـ]")
_ARABIC_LETTERS = str.maketrans({
    "أ": "ا",  # alef with hamza above -> alef
    "إ": "ا",  # alef with hamza below -> alef
    "آ": "ا",  # alef with madda -> alef
    "ٱ": "ا",  # alef wasla -> alef
    "ى": "ي",  # alef maksura -> yeh
})
_NON_WORD = re.compile(r"[^\w\s]|_")
_SPACES = re.compile(r"\s+")

# Websites and social handles are never entities.
_HANDLE = re.compile(
    r"^(@|www\.)|://|_|\.(com|net|org|ae|sa|qa|om|kw|bh|io|co|me|info|dubai)$",
    re.IGNORECASE,
)


def normalize_alias(text: str) -> str:
    """Returns the comparison form of a name. Spaces are collapsed, never removed."""
    s = unicodedata.normalize("NFKC", text or "")
    s = _ARABIC_MARKS.sub("", s)
    s = s.translate(_ARABIC_LETTERS)
    s = s.casefold()
    s = _NON_WORD.sub(" ", s)
    return _SPACES.sub(" ", s).strip()


def raw_key(text: str) -> str:
    """Case-insensitive original string, used for the ignore list (checked before normalising)."""
    return _SPACES.sub(" ", unicodedata.normalize("NFKC", text or "")).strip().casefold()


def looks_like_handle(text: str) -> bool:
    """True for strings that look like a website or a social account."""
    return bool(_HANDLE.search((text or "").strip()))
