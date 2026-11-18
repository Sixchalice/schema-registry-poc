from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from schema_validator import messages
from schema_validator._text import shorten
from schema_validator.formats.protobuf import _clean_stderr, _imports, analyse
from schema_validator.models import DEFAULT_BUILTIN_PROTO_IMPORTS, FormatAnalysis, ValidatorConfig

P = 'syntax = "proto3";\npackage com.acme;\n'
NO_PACKAGE = ['Protobuf schema needs a package, for example "package com.team.domain;".']
TIMEOUT = [messages.PROTO_TIMEOUT.format(seconds=10)]
ADDRESS = P + "message Address { string street = 1; }"


def run(text: str, referenced: dict[Any, Any] | None = None) -> FormatAnalysis:
    return analyse(text, {} if referenced is None else referenced, ValidatorConfig())


def message_count(n: int) -> list[str]:
    return [
        f"A Protobuf schema must define exactly one top-level message (found {n}). "
        "Nest the others inside it or move them to their own Schema."
    ]


@pytest.fixture
def temp_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Create every TemporaryDirectory inside tmp_path and record its path."""
    real = tempfile.TemporaryDirectory
    seen: list[str] = []

    def recording(*args: Any, **kwargs: Any) -> tempfile.TemporaryDirectory[str]:
        kwargs["dir"] = str(tmp_path)
        td = real(*args, **kwargs)
        seen.append(td.name)
        return td

    monkeypatch.setattr(tempfile, "TemporaryDirectory", recording)
    return seen


def test_valid() -> None:
    result = run(P + "message Order { string id = 1; }")
    assert result == FormatAnalysis("com.acme.Order", set(), [])


def test_syntax_error(temp_dirs: list[str]) -> None:
    result = run(P + "message Order { strin id = 1 }")
    assert result.expected_name is None
    assert len(result.errors) == 1
    error = result.errors[0]
    assert error.startswith("Invalid Protobuf schema: ")
    assert 'Expected ";"' in error
    assert "__main__" not in error
    assert temp_dirs and temp_dirs[0] not in error
    assert os.path.realpath(temp_dirs[0]) not in error


def test_no_package() -> None:
    result = run('syntax = "proto3";\nmessage Order { string id = 1; }')
    assert result.errors == NO_PACKAGE
    assert result.expected_name is None


def test_no_package_and_two_messages() -> None:
    result = run('syntax = "proto3";\nmessage A { string a = 1; }\nmessage B { string b = 1; }')
    assert result.errors == NO_PACKAGE + message_count(2)
    assert result.expected_name is None


def test_two_messages() -> None:
    result = run(P + "message A { string a = 1; }\nmessage B { string b = 1; }")
    assert result.errors == message_count(2)
    assert result.expected_name is None


def test_zero_messages() -> None:
    result = run(P + "enum E { A = 0; }")
    assert result.errors == message_count(0)
    assert result.expected_name is None


def test_nested_message_and_top_level_enum_allowed() -> None:
    result = run(
        P + "enum Status { UNKNOWN = 0; }\n"
        "message Order { message Inner { string x = 1; } Inner inner = 1; Status status = 2; }"
    )
    assert result == FormatAnalysis("com.acme.Order", set(), [])


def test_builtin_imports_need_no_reference() -> None:
    result = run(
        P + 'import "google/protobuf/timestamp.proto";\n'
        'import "google/type/date.proto";\n'
        'import "confluent/meta.proto";\n'
        "message Order {\n"
        "  google.protobuf.Timestamp at = 1;\n"
        "  google.type.Date day = 2;\n"
        '  string id = 3 [(confluent.field_meta).doc = "id"];\n'
        "}"
    )
    assert result == FormatAnalysis("com.acme.Order", set(), [])


def test_every_builtin_compiles() -> None:
    assert len(DEFAULT_BUILTIN_PROTO_IMPORTS) == 29
    for path in sorted(DEFAULT_BUILTIN_PROTO_IMPORTS):
        result = run(P + f'import "{path}";\nmessage M {{ string a = 1; }}')
        assert result == FormatAnalysis("com.acme.M", set(), []), path


def test_reference() -> None:
    result = run(
        P + 'import "com/acme/Address.proto";\nmessage Customer { Address address = 1; }',
        {"com/acme/Address.proto": ADDRESS},
    )
    assert result == FormatAnalysis("com.acme.Customer", {"com/acme/Address.proto"}, [])


def test_transitive_reference() -> None:
    b = 'syntax = "proto3";\npackage a;\nimport "a/C.proto";\nmessage B { C c = 1; }'
    c = 'syntax = "proto3";\npackage a;\nmessage C { string x = 1; }'
    result = run(
        P + 'import "a/B.proto";\nmessage Order { a.B b = 1; }',
        {"a/B.proto": b, "a/C.proto": c},
    )
    assert result == FormatAnalysis("com.acme.Order", {"a/B.proto"}, [])


def test_missing_import_left_to_stage_d() -> None:
    result = run(P + 'import "com/acme/Address.proto";\nmessage Customer { Address address = 1; }')
    assert result == FormatAnalysis(None, {"com/acme/Address.proto"}, [])


B_IMPORTS_C = 'syntax = "proto3";\npackage a;\nimport "a/C.proto";\nmessage B { C c = 1; }'


def test_missing_transitive_import_is_invalid() -> None:
    result = run(P + 'import "a/B.proto";\nmessage Order { a.B b = 1; }', {"a/B.proto": B_IMPORTS_C})
    assert result.expected_name is None
    assert result.external_refs is None
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Invalid Protobuf schema: ")
    assert "a/C.proto" in result.errors[0]


def test_missing_own_and_transitive_imports_is_invalid() -> None:
    result = run(
        P + 'import "x/Y.proto";\nimport "a/B.proto";\nmessage Order { a.B b = 1; x.Y y = 2; }',
        {"a/B.proto": B_IMPORTS_C},
    )
    assert result.expected_name is None
    assert result.external_refs is None
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Invalid Protobuf schema: ")
    assert "a/C.proto" in result.errors[0]


def test_commented_import_ignored() -> None:
    result = run(
        P + '// import "old/x.proto";\n'
        '/* import "old/y.proto"; */\n'
        'import "com/acme/Address.proto";\n'
        "message Customer { Address address = 1; }"
    )
    assert result.external_refs == {"com/acme/Address.proto"}
    assert result.errors == []


def test_import_kinds_and_builtins_on_failure() -> None:
    result = run(
        P + 'import public "x/P.proto";\n'
        "import weak 'x/W.proto';\n"
        'import "google/protobuf/timestamp.proto";\n'
        'option java_package = "http://not/a/comment";\n'
        "message Customer { string a = 1; }"
    )
    assert result.external_refs == {"x/P.proto", "x/W.proto"}
    assert result.errors == []
    assert result.expected_name is None


def test_non_builtin_google_import_is_external() -> None:
    result = run(P + 'import "google/rpc/status.proto";\nmessage M { string a = 1; }')
    assert result.external_refs == {"google/rpc/status.proto"}
    assert result.errors == []


def test_error_in_referenced_file_is_invalid() -> None:
    result = run(
        P + 'import "com/acme/Address.proto";\nmessage Customer { Address address = 1; }',
        {"com/acme/Address.proto": P + "message Address { strin x = 1 }"},
    )
    assert result.expected_name is None
    assert result.external_refs is None
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Invalid Protobuf schema: ")
    assert "com/acme/Address.proto:" in result.errors[0]


@pytest.mark.parametrize("key", ["/etc/x.proto", "../x.proto", "a/../../x.proto", "a\\b.proto", "", "a\0.proto"])
def test_unsafe_reference_paths(
    key: str, tmp_path: Path, temp_dirs: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_protoc(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("protoc must not run")

    monkeypatch.setattr(subprocess, "run", no_protoc)
    result = run(
        P + 'import "com/acme/Address.proto";\nmessage Customer { Address address = 1; }',
        {"com/acme/Address.proto": ADDRESS, key: ADDRESS},
    )
    assert result == FormatAnalysis(None, None, [f"Invalid import path '{key}'."])
    # Every temp directory lives in tmp_path and is gone, so nothing else may remain there.
    assert list(tmp_path.iterdir()) == []
    assert not Path("/etc/x.proto").exists()


def test_reference_key_not_a_string() -> None:
    result = run(P + "message M { string a = 1; }", {42: ADDRESS})
    assert result == FormatAnalysis(None, None, ["Invalid import path '42'."])


def test_reference_paths_that_cannot_be_written() -> None:
    result = run(P + "message M { string a = 1; }", {"a": ADDRESS, "a/b.proto": ADDRESS})
    assert result == FormatAnalysis(None, None, ["Invalid import path 'a/b.proto'."])


def test_reference_text_not_a_string() -> None:
    result = run(P + "message M { string a = 1; }", {"a/B.proto": None})
    assert result.expected_name is None
    assert result.external_refs is None
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Internal validator error: ")


@pytest.mark.parametrize("key", ["__main__.proto", "__main__.proto/x.proto"])
def test_reference_colliding_with_the_main_file(key: str) -> None:
    result = run(P + "message M { string a = 1; }", {key: ADDRESS})
    assert result == FormatAnalysis(None, None, [f"Invalid import path '{key}'."])


def test_nul_in_schema_text() -> None:
    result = run(P + "message M { string\0 a = 1; }")
    assert result.expected_name is None
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Invalid Protobuf schema: ")


def test_text_not_encodable() -> None:
    result = run(P + "message M { string a = 1; } // \ud800")
    assert result.expected_name is None
    assert result.external_refs is None
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Invalid Protobuf schema: ")


def test_reference_text_not_encodable() -> None:
    result = run(P + "message M { string a = 1; }", {"a/B.proto": "// \udcff"})
    assert result.external_refs is None
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Invalid Protobuf schema: a/B.proto: ")


def test_warnings_do_not_fail() -> None:
    no_syntax = run("package com.acme;\nmessage M { optional string a = 1; }")
    assert no_syntax == FormatAnalysis("com.acme.M", set(), [])
    unused = run(P + 'import "google/type/date.proto";\nmessage M { string a = 1; }')
    assert unused == FormatAnalysis("com.acme.M", set(), [])


def test_temp_directory_removed(temp_dirs: list[str]) -> None:
    assert run(P + "message M { string a = 1; }").errors == []
    assert run(P + "message M { strin a = 1 }").errors != []
    assert len(temp_dirs) == 2
    for path in temp_dirs:
        assert not os.path.exists(path)


def test_protoc_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(*args: Any, **kwargs: Any) -> None:
        assert kwargs["timeout"] == 10
        raise subprocess.TimeoutExpired(args[0], 10)

    monkeypatch.setattr(subprocess, "run", slow)
    assert run(P + "message M { string a = 1; }") == FormatAnalysis(None, None, TIMEOUT)


def test_protoc_cannot_start(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(subprocess, "run", missing)
    result = run(P + "message M { string a = 1; }")
    assert result.expected_name is None
    assert result.external_refs is None
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Internal validator error: ")


@pytest.mark.parametrize("stderr", ["", "\n", "__main__.proto: warning: x\n"], ids=["empty", "blank", "warnings-only"])
def test_protoc_failure_without_message_is_internal(monkeypatch: pytest.MonkeyPatch, stderr: str) -> None:
    # A crash or a signal: protoc exits non-zero without an error line.
    def crashed(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=-9, stdout="", stderr=stderr)

    monkeypatch.setattr(subprocess, "run", crashed)
    result = run(P + "message M { string a = 1; }")
    assert result.expected_name is None
    assert result.external_refs is None
    assert result.errors == ["Internal validator error: protoc exited with code -9 and no message"]


def _fake_protoc(monkeypatch: pytest.MonkeyPatch, out: bytes | None) -> None:
    """protoc that exits 0 and writes `out` (or nothing) as the descriptor set."""

    def fake(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if out is not None:
            target = next(a for a in cmd if a.startswith("--descriptor_set_out=")).partition("=")[2]
            Path(target).write_bytes(out)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake)


@pytest.mark.parametrize(
    "out",
    [
        None,  # no descriptor written
        b"\xff\xff\xff",  # not a FileDescriptorSet
        # A valid set whose only file is not __main__.proto.
        bytes.fromhex("0a0d0a0b6f746865722e70726f746f"),
    ],
    ids=["absent", "garbage", "no-main-entry"],
)
def test_unusable_descriptor(monkeypatch: pytest.MonkeyPatch, out: bytes | None) -> None:
    _fake_protoc(monkeypatch, out)
    result = run(P + "message M { string a = 1; }")
    assert result.expected_name is None
    assert result.external_refs is None
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Internal validator error: ")


def test_empty_descriptor_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    # FileDescriptorSet { file { name: "__main__.proto" } }: no package, no messages.
    _fake_protoc(monkeypatch, bytes.fromhex("0a100a0e5f5f6d61696e5f5f2e70726f746f"))
    result = run(P + "message M { string a = 1; }")
    assert result == FormatAnalysis(None, set(), NO_PACKAGE + message_count(0))


def _nested_messages(levels: int) -> str:
    text = "int32 x = 1;"
    for n in range(levels - 1, -1, -1):
        text = f"message M{n} {{ {text} }}"
    return P + text


def test_protobuf_nesting_limit_is_protocs() -> None:
    # protoc stops at 31 nested message definitions; the registry would accept 500.
    assert run(_nested_messages(31)).errors == []
    result = run(_nested_messages(32))
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Invalid Protobuf schema: ")
    assert "Reached maximum recursion limit for nested messages." in result.errors[0]


def test_unclosed_comments_are_fast_and_short() -> None:
    text = 'syntax = "proto3";\npackage a.b;\nmessage M { int32 x = 1; }\n' + "/* " * 160000
    started = time.monotonic()
    result = analyse(text, {}, ValidatorConfig())
    assert time.monotonic() - started < 10
    assert len(result.errors) == 1 and result.errors[0].startswith("Invalid Protobuf schema: ")
    assert len(result.errors[0]) <= len("Invalid Protobuf schema: ") + 1000
    assert "more" in result.errors[0]


def test_imports_scan_is_linear() -> None:
    # 240 KB took 37 s with the old pattern.
    started = time.monotonic()
    assert _imports('import "a/b.proto";\n' + "/* " * 80000) == {"a/b.proto"}
    assert time.monotonic() - started < 1


@pytest.mark.parametrize("quote", ['"', "'"], ids=["double", "single"])
def test_unclosed_escaped_strings_are_fast(quote: str) -> None:
    # An unclosed string of escaped quotes: 80 KB took 10 s with the old pattern, 480 KB about 6 min.
    text = 'syntax = "proto3";\npackage a.b;\nimport "a/b.proto";\nmessage M { int32 x = 1; }\n'
    text += quote + ("\\" + quote) * 240000
    started = time.monotonic()
    assert _imports(text) == {"a/b.proto"}
    assert time.monotonic() - started < 1
    started = time.monotonic()
    result = analyse(text, {}, ValidatorConfig())
    assert time.monotonic() - started < 10
    assert len(result.errors) == 1 and result.errors[0].startswith("Invalid Protobuf schema: ")
    assert result.external_refs is None


def test_closed_strings_are_unchanged_by_the_scan() -> None:
    # A closed string, also one continued with a backslash at the end of a line, hides "//" and "/*".
    assert _imports('option o = "a // b";\nimport "a/b.proto";') == {"a/b.proto"}
    assert _imports("option o = 'a /* b';\nimport \"a/b.proto\";") == {"a/b.proto"}
    assert _imports('option o = "a \\\n /* b";\nimport "a/b.proto";') == {"a/b.proto"}
    assert _imports('option o = "a \\" /* b";\nimport "a/b.proto";') == {"a/b.proto"}


def test_unclosed_string_ends_at_the_end_of_its_line() -> None:
    assert _imports('option o = "a /* b\nimport "a/b.proto";') == {"a/b.proto"}


def test_import_after_a_closed_comment_is_found() -> None:
    assert _imports('/* import "x/y.proto"; */ import "a/b.proto";') == {"a/b.proto"}


def test_six_errors_show_five_and_one_more() -> None:
    lines = [f"__main__.proto:{i}:1: Expected type name." for i in range(1, 7)]
    assert _clean_stderr(lines, "/tmp/x") == "; ".join(
        f"{i}:1: Expected type name." for i in range(1, 6)) + "; and 1 more"


def test_shorten() -> None:
    assert shorten("abc", 3) == "abc"
    assert shorten("abcd ef", 6) == "abcd…"


def test_main_file_name_is_your_schema() -> None:
    result = run(
        P.replace("com.acme", "aud.o") + 'import "aud/q/Wrap.proto";\nmessage O { aud.p.Geo g = 1; }',
        {"aud/q/Wrap.proto": 'syntax = "proto3";\npackage aud.q;\nimport "aud/p/Geo.proto";\nmessage Wrap { aud.p.Geo g = 1; }',
         "aud/p/Geo.proto": 'syntax = "proto3";\npackage aud.p;\nmessage Geo { string a = 1; }'},
    )
    assert result.errors == [
        'Invalid Protobuf schema: 4:13: "aud.p.Geo" seems to be defined in "aud/p/Geo.proto", '
        "which is not imported by your schema.  To use it here, please add the necessary import."
    ]


def test_python_executable_is_configurable() -> None:
    config = ValidatorConfig(python_executable="/nonexistent/python")
    result = analyse(P + "message M { string a = 1; }", {}, config)
    assert result.external_refs is None
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Internal validator error: ")


def test_protoc_timeout_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(*args: Any, **kwargs: Any) -> None:
        assert kwargs["timeout"] == 2.5
        raise subprocess.TimeoutExpired(args[0], 2.5)

    monkeypatch.setattr(subprocess, "run", slow)
    result = analyse(P + "message M { string a = 1; }", {}, ValidatorConfig(protoc_timeout_seconds=2.5))
    assert result.errors == [messages.PROTO_TIMEOUT.format(seconds=2.5)]
    assert result.errors[0].endswith("did not finish in 2.5 seconds.")
