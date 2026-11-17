"""Small text helpers."""
from __future__ import annotations


def shorten(text: str, limit: int) -> str:
    """`text` unchanged when it fits in `limit` characters, otherwise cut with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
