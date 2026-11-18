"""Assembles the final ValidationResult from collected messages."""
from __future__ import annotations

from .models import ValidationResult


def _unique(messages: list[str]) -> list[str]:
    return list(dict.fromkeys(messages))


def _utf8(message: str) -> str:
    """`message` with each lone surrogate (from a `\\ud800` escape in the schema, echoed by a
    parser) written as its backslash escape, so the message always encodes as UTF-8."""
    return message.encode("utf-8", "backslashreplace").decode("utf-8")


def build_result(errors: list[str], warnings: list[str]) -> ValidationResult:
    if errors:
        return ValidationResult(False, "Error", _utf8("\n".join(_unique(errors))))
    if warnings:
        return ValidationResult(True, "Warning", _utf8("\n".join(_unique(warnings))))
    return ValidationResult(True, "Ok", "")
