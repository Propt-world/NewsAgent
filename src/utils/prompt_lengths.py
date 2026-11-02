"""
Summary and "why this matters" lengths come from settings (.env) through the workflow state, so they can be
changed without a code change or an init_db run. The registry prompts hold placeholders that are filled here:

  {summary_min_words} {summary_max_words} {summary_sentences}
  {wtm_min_words} {wtm_max_words} {wtm_sentences}

Filled with plain text replacement before any template formatting, so the placeholders are never read as
template inputs. A prompt without them is returned unchanged.
"""
# About 20-22 words a sentence (Propt's summaries); used to turn a word range into a sentence range
WORDS_PER_SENTENCE = 21


def sentence_range(min_words: int, max_words: int) -> str:
    """e.g. 110-130 words -> "5-6" sentences."""
    low = max(1, round(min_words / WORDS_PER_SENTENCE))
    high = max(low, round(max_words / WORDS_PER_SENTENCE))
    return f"{low}-{high}" if high > low else str(low)


def fill_lengths(text: str, state) -> str:
    if not text:
        return text
    values = {
        "summary_min_words": state.summary_min_words,
        "summary_max_words": state.summary_max_words,
        "summary_sentences": sentence_range(state.summary_min_words, state.summary_max_words),
        "wtm_min_words": state.wtm_min_words,
        "wtm_max_words": state.wtm_max_words,
        "wtm_sentences": sentence_range(state.wtm_min_words, state.wtm_max_words),
    }
    for name, value in values.items():
        text = text.replace("{" + name + "}", str(value))
    return text
