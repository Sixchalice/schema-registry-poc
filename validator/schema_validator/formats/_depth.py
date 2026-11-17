"""Nesting depth of a parsed JSON value, without recursion."""
from __future__ import annotations

from typing import Any


def json_depth(value: Any, limit: int) -> int:
    """How deeply objects and arrays are nested; a scalar is 0 and `{"a": 1}` is 1.

    Stops as soon as the depth passes `limit`, and then returns a number above it.
    """
    deepest = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            children: Any = item.values()
        elif isinstance(item, list):
            children = item
        else:
            continue
        depth += 1
        if depth > limit:
            return depth
        deepest = max(deepest, depth)
        stack.extend((child, depth) for child in children)
    return deepest
