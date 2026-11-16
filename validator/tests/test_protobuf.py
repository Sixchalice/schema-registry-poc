from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from schema_validator.formats.protobuf import analyse
from schema_validator.models import DEFAULT_BUILTIN_PROTO_IMPORTS, FormatAnalysis, ValidatorConfig

P = 'syntax = "proto3";\npackage com.acme;\n'
NO_PACKAGE = ['Protobuf schema needs a package, for example "package com.team.domain;".']
TIMEOUT = ["Invalid Protobuf schema: the Protobuf compiler did not finish in 10 seconds."]
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
    assert result.external_refs == {"com/acme/Address.proto"}
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
