import unicodedata

from .sentence_splitter import split_text_by_punctuation


class TextSegmenter:
    """Split text with punctuation-first boundaries for on-screen display."""

    def __init__(self, max_chars_per_segment: int = 20) -> None:
        self.max_chars_per_segment = max_chars_per_segment

    def segment(self, text: str) -> list[str]:
        segments = split_text_by_punctuation(text, max_chars_per_segment=self.max_chars_per_segment)
        # Protect symbols with an alpha-only marker so punctuation stripping does not corrupt it.
        protected = [s.replace("π", "GREEKCONSTANTMARKER") for s in segments]
        cleaned = [self._clean_for_display(s) for s in protected]
        # Restore and fallback
        final = [s.replace("GREEKCONSTANTMARKER", "π") for s in cleaned]
        return [s for s in final if s]

    def _clean_for_display(self, segment: str) -> str:
        chars = [ch for ch in segment if not unicodedata.category(ch).startswith("P")]
        return "".join(chars).strip()
