"""Parses the output of `sr-acl-cli --list`:

    Current ACL's for Subject Operations are
    {p1={orders-key=[SUBJECT_READ], *=[SUBJECT_WRITE]}, p2={}}
    Current ACL's for Global Operations are
    {p1=[GLOBAL_READ]}

The maps are Java's Map.toString(), which has no quoting.
"""

import re

from acl_api.domain.models import AclSnapshot
from acl_api.errors import UnexpectedCliOutput

_SUBJECT_HEADER = "Current ACL's for Subject Operations"
_GLOBAL_HEADER = "Current ACL's for Global Operations"


def parse_list_output(stdout: str) -> AclSnapshot:
    lines = [line.strip() for line in stdout.splitlines()]
    try:
        return AclSnapshot(
            subject_operations=_map_after(_SUBJECT_HEADER, lines),
            global_operations=_map_after(_GLOBAL_HEADER, lines),
        )
    except (ValueError, IndexError) as error:
        raise UnexpectedCliOutput(f"could not parse sr-acl-cli --list output: {error}") from error


def _map_after(header: str, lines: list[str]) -> dict:
    """The map printed on the line after `header` ({} if that line is not a map)."""
    index = next((i for i, line in enumerate(lines) if line.startswith(header)), None)
    if index is None:
        raise ValueError(f"no '{header}' line")
    following = lines[index + 1] if index + 1 < len(lines) else ""
    return JavaMapParser(following).parse() if following.startswith("{") else {}


class JavaMapParser:
    """Parses Java's Map.toString() of nested maps and lists of strings.

    A key runs up to the next '={' or '=[', so keys may contain ', ' and '='. Only a key that itself
    contains '={' or '=[' would be misread.
    """

    _KEY_END = re.compile(r"=(?=[{\[])")

    def __init__(self, text: str):
        self._text = text
        self._pos = 0

    def parse(self) -> dict | list:
        value = self._value()
        if self._pos != len(self._text):
            raise ValueError(f"unexpected text at position {self._pos}")
        return value

    def _value(self) -> dict | list:
        if self._at("{"):
            return self._map()
        if self._at("["):
            return self._list()
        raise ValueError(f"expected '{{' or '[' at position {self._pos}")

    def _map(self) -> dict:
        self._pos += 1  # '{'
        result: dict = {}
        if self._consume("}"):
            return result
        while True:
            key = self._key()
            result[key] = self._value()
            if self._consume("}"):
                return result
            if not self._consume(", "):
                raise ValueError(f"expected ', ' or '}}' at position {self._pos}")

    def _key(self) -> str:
        end = self._KEY_END.search(self._text, self._pos)
        if end is None:
            raise ValueError(f"expected 'key=' at position {self._pos}")
        key = self._text[self._pos:end.start()]
        self._pos = end.end()
        return key

    def _list(self) -> list[str]:
        end = self._text.index("]", self._pos)
        items = self._text[self._pos + 1:end]
        self._pos = end + 1
        return [item for item in items.split(", ") if item]

    def _at(self, token: str) -> bool:
        return self._text.startswith(token, self._pos)

    def _consume(self, token: str) -> bool:
        if self._at(token):
            self._pos += len(token)
            return True
        return False
