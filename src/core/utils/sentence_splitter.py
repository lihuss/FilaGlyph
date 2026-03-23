from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
import re

from .error_logging import log_runtime_error


PUNCTUATION_SET = set("。！？!?；;，,、：:")
CLOSING_PREFIX_SET = set("”’）】》」』〉〕")


def _normalize_text(text: str) -> str:
    # Keep line breaks as hard boundaries while normalizing other spaces.
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[\t\f\v ]+", " ", text)
    return text.strip()


def _chunk_long_piece(piece: str, max_chars: int) -> list[str]:
    if len(piece) <= max_chars:
        return [piece]

    max_chars = max(2, max_chars)
    trailing_punct = piece[-1] if piece and piece[-1] in PUNCTUATION_SET else ""
    body = piece[:-1] if trailing_punct else piece

    chunks: list[str] = []
    start = 0
    while len(body) - start > max_chars:
        end = start + max_chars
        remain = len(body) - end
        # Avoid one-character orphan tails like "...规" + "律".
        if remain == 1:
            end -= 1
        chunks.append(body[start:end])
        start = end

    tail = body[start:]
    if tail:
        chunks.append(tail)

    if trailing_punct and chunks:
        chunks[-1] = f"{chunks[-1]}{trailing_punct}"
    return [chunk for chunk in chunks if chunk]


def split_text_by_punctuation(text: str, max_chars_per_segment: int = 20) -> list[str]:
    normalized = _normalize_text(text)
    if not normalized:
        return []

    raw_segments: list[str] = []
    current: list[str] = []

    for char in normalized:
        if char == "\n":
            piece = "".join(current).strip()
            if piece:
                raw_segments.append(piece)
            current = []
            continue

        current.append(char)
        if char in PUNCTUATION_SET:
            piece = "".join(current).strip()
            if piece:
                raw_segments.append(piece)
            current = []

    tail = "".join(current).strip()
    if tail:
        raw_segments.append(tail)

    segments: list[str] = []
    for piece in raw_segments:
        segments.extend(_chunk_long_piece(piece, max_chars=max_chars_per_segment))
    return _stitch_closing_prefix(segments)


def _stitch_closing_prefix(segments: list[str]) -> list[str]:
    if not segments:
        return []

    fixed: list[str] = []
    for segment in segments:
        current = segment
        if fixed and current:
            i = 0
            while i < len(current) and current[i] in CLOSING_PREFIX_SET:
                i += 1
            if i > 0:
                fixed[-1] = f"{fixed[-1]}{current[:i]}"
                current = current[i:]

        if current:
            fixed.append(current)
    return fixed


def _build_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Split Chinese text into punctuation-first segments.")
    parser.add_argument("--text", type=str, help="Input text")
    parser.add_argument("--text-file", type=str, help="Path to UTF-8 text file")
    parser.add_argument(
        "--max-chars",
        type=int,
        default=20,
        help="Max chars per segment when no punctuation split is available",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="",
        help="Optional output path. If omitted, print to stdout.",
    )
    return parser


def _read_text(args) -> str:
    if args.text:
        return args.text
    if args.text_file:
        return Path(args.text_file).read_text(encoding="utf-8")
    raise ValueError("Provide --text or --text-file")


def main() -> None:
    args = _build_parser().parse_args()
    segments = split_text_by_punctuation(_read_text(args), max_chars_per_segment=args.max_chars)
    content = "\n".join(segments)

    if args.output:
        Path(args.output).write_text(content, encoding="utf-8")
    else:
        print(content)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log_path = log_runtime_error("src/core/utils/sentence_splitter.py", exc)
        print(f"Error details written to: {log_path}")
        raise
