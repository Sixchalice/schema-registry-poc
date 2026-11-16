"""Parsing of the verdict messages returned by POST /compatibility (spec §7).

The text comes from a remote service, so neither function raises: input that does not
have the expected shape is passed through unchanged (or ignored when it is not text).
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

_INVALID_PREFIX = "Invalid schema "
_DETAILS = "details: "
_OLD_VERSION = re.compile(r"^\{oldSchemaVersion: (\d+)\}$")
_LEVEL = re.compile(r"compatibility: '([A-Z_]+)'\}$")
_DESCRIPTION = ", description:"
_ADDITIONAL_INFO = ", additionalInfo:"
_QUOTES = ("'", '"')


def invalid_schema_reason(message: str) -> str | None:
    """None unless `message` is an "Invalid schema " message; otherwise the text after the
    last "details: ", or "" when that is absent or blank. The rest of the message embeds
    the submitted schema, so it is never returned."""
    if not isinstance(message, str) or not message.startswith(_INVALID_PREFIX):
        return None
    _, found, reason = message.rpartition(_DETAILS)
    if not found or not reason.strip():
        return ""
    return reason


@dataclass(frozen=True)
class Incompatibility:
    descriptions: tuple[str, ...]  # extracted descriptions, then messages that had no recognised shape
    old_version: int | None
    level: str | None


def parse_incompatibility(messages: Sequence[str]) -> Incompatibility:
    if isinstance(messages, str):
        messages = [messages]
    try:
        items = list(messages)
    except TypeError:
        items = []

    extracted: list[str] = []
    unrecognised: list[str] = []
    old_version: int | None = None
    level: str | None = None

    for message in items:
        if not isinstance(message, str) or not message.strip():
            continue
        version = _old_version(message)
        if version is not None:
            old_version = version
            continue
        # The registered schema's text: never shown, even if the format gains whitespace.
        if message.lstrip().startswith("{oldSchema:"):
            continue
        if message.startswith("{validateFields"):
            match = _LEVEL.search(message)
            if match:
                level = match.group(1)
                continue
        if message.startswith("{errorType:"):
            description = _description(message)
            if description is not None:
                extracted.append(description)
                continue
        unrecognised.append(message)

    return Incompatibility(tuple(extracted + unrecognised), old_version, level)


def _old_version(message: str) -> int | None:
    match = _OLD_VERSION.match(message)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:  # more digits than int() accepts
        return None


def _description(message: str) -> str | None:
    """The description of an `{errorType:..., description:...}` message, or None when the
    message does not have that shape."""
    start = message.find(_DESCRIPTION)
    end = message.rfind("}")
    if start < 0 or end < start + len(_DESCRIPTION):
        return None
    text = message[start + len(_DESCRIPTION):end]
    cut = text.rfind(_ADDITIONAL_INFO)
    if cut >= 0:
        text = text[:cut]
    if text.startswith(_QUOTES):
        text = text[1:]
    if text.endswith(_QUOTES):
        text = text[:-1]
    return text if text.strip() else None
