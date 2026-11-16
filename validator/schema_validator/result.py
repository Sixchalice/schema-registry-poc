"""Assembles the final ValidationResult from collected messages."""
from __future__ import annotations

from .models import ValidationResult


def _unique(messages: list[str]) -> list[str]:
    return list(dict.fromkeys(messages))


def build_result(errors: list[str], warnings: list[str]) -> ValidationResult:
    if errors:
        return ValidationResult(False, "Error", "\n".join(_unique(errors)))
    if warnings:
        return ValidationResult(True, "Warning", "\n".join(_unique(warnings)))
    return ValidationResult(True, "Ok", "")
