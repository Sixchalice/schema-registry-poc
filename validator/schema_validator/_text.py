"""Small text helpers."""
from __future__ import annotations

import ast
import json


def shorten(text: str, limit: int) -> str:
    """`text` unchanged when it fits in `limit` characters, otherwise cut with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


_MAX_TAILS = 3  # `: {` or `: [` starts tried for the trailing literal
_LEADING_MARKERS = (" is not ", " is too ")


def _literal_as_json(text: str) -> str | None:
    """`text` as a Python literal written as JSON, or None when it is not one (never raises)."""
    try:
        return json.dumps(ast.literal_eval(text), ensure_ascii=False)
    except Exception:  # SyntaxError, ValueError, TypeError, RecursionError, MemoryError ...
        return None


def _angle_segments(text: str) -> str:
    """Each `<literal>` in `text` written as JSON; every other character is kept.

    One left-to-right pass: each `>` pairs with the nearest `<` before it, so every
    character is scanned a bounded number of times and a literal containing `<` is not tried.
    """
    out: list[str] = []
    pos = 0  # start of the text not yet copied
    while True:
        end = text.find(">", pos)
        if end < 0:
            break
        start = text.rfind("<", pos, end)
        if start >= 0:
            converted = _literal_as_json(text[start + 1 : end])
            if converted is not None:
                out.append(text[pos:start])
                out.append(converted)
                pos = end + 1
                continue
        # Not a literal (or no `<` before this `>`): keep the text up to the `>` and go on.
        out.append(text[pos : end + 1])
        pos = end + 1
    out.append(text[pos:])
    return "".join(out)


def json_values(text: str) -> str:
    """A parser message with the Python literals in it written as JSON.

    fastavro and jsonschema print Python values: `None`, `True`, `{'type': 'int'}`. This
    converts `<literal>` segments, a trailing `: {...}` or `: [...]`, and a leading
    literal before " is not " or " is too ". What it cannot read it leaves as it is.
    It never raises and takes time linear in the text.
    """
    tail = ""
    scan = 0
    for _ in range(_MAX_TAILS):
        hits = [i for i in (text.find(": {", scan), text.find(": [", scan)) if i >= 0]
        if not hits:
            break
        start = min(hits)
        converted = _literal_as_json(text[start + 2 :])
        if converted is not None:
            text, tail = text[: start + 2], converted
            break
        scan = start + 1
    for marker in _LEADING_MARKERS:
        idx = text.find(marker)
        if idx > 0:
            converted = _literal_as_json(text[:idx])
            if converted is not None:
                return converted + _angle_segments(text[idx:]) + tail
    return _angle_segments(text) + tail
