"""Stage B for Protobuf (spec section 5.3)."""
from __future__ import annotations

import importlib.resources
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from google.protobuf import descriptor_pb2
from google.protobuf.message import DecodeError

from schema_validator import messages
from schema_validator._text import shorten
from schema_validator.models import FormatAnalysis, ValidatorConfig

_MAIN = "__main__.proto"
_DESCRIPTOR_SET = "out.pb"
_PROTOC_TIMEOUT_SECONDS = 10
_PROTOC_ERRORS_SHOWN = 5
_PROTOC_MESSAGE_LIMIT = 1000

# String literals are matched too, so that "//" inside a string is not taken for a comment.
# An unclosed string runs to the end of its line and an unclosed comment to the end of the
# text, so each match is tried once from each start and the scan is linear.
_STRING_OR_COMMENT = re.compile(
    r'"(?:\\.|[^"\\\n])*"?|\'(?:\\.|[^\'\\\n])*\'?|//[^\n]*|/\*.*?(?:\*/|\Z)', re.DOTALL
)
_IMPORT = re.compile(r'\bimport\s+(?:(?:public|weak)\s+)?(?:"([^"\n]*)"|\'([^\'\n]*)\')\s*;')
_NOT_FOUND = re.compile(r"(.+): File not found\.")


def analyse(schema_text: str, referenced: dict[str, str], config: ValidatorConfig) -> FormatAnalysis:
    """Steps 1 to 10 of spec 5.3. `referenced` maps import paths to schema texts (direct and transitive)."""
    # Step 3 (security), before anything is written.
    for path, text in referenced.items():
        if not _is_safe_path(path):
            return FormatAnalysis(None, None, [messages.PROTO_IMPORT_PATH.format(x=path)])
        if not isinstance(text, str):
            return FormatAnalysis(
                None, None, [messages.INTERNAL_ERROR.format(err=f"{path}: {type(text).__name__} is not text")]
            )
    try:
        main_bytes = schema_text.encode("utf-8")
    except UnicodeEncodeError as e:
        return FormatAnalysis(None, None, [messages.PROTO_INVALID.format(err=e)])
    files: dict[str, bytes] = {}
    for path, text in referenced.items():
        try:
            files[path] = text.encode("utf-8")
        except UnicodeEncodeError as e:
            return FormatAnalysis(None, None, [messages.PROTO_INVALID.format(err=f"{path}: {e}")])

    # Step 1
    with tempfile.TemporaryDirectory() as tmp:
        # Steps 2 and 3. Exclusive creation: a reference cannot replace the main file or clash with a directory.
        Path(tmp, _MAIN).write_bytes(main_bytes)
        for path, data in files.items():
            target = Path(tmp, path)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as f:
                    f.write(data)
            except OSError:
                return FormatAnalysis(None, None, [messages.PROTO_IMPORT_PATH.format(x=path)])

        # Step 4
        command = [
            sys.executable, "-m", "grpc_tools.protoc",
            "-I", tmp,
            "-I", str(importlib.resources.files("grpc_tools") / "_proto"),
            "-I", str(importlib.resources.files("schema_validator") / "_proto"),
            "--include_imports",
            f"--descriptor_set_out={tmp}/{_DESCRIPTOR_SET}",
            _MAIN,
        ]
        try:
            # cwd=tmp: protoc must not pick up a __main__.proto from the caller's directory.
            completed = subprocess.run(
                command, cwd=tmp, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=_PROTOC_TIMEOUT_SECONDS, check=False,
            )
        except subprocess.TimeoutExpired:
            return FormatAnalysis(None, None, [messages.PROTO_TIMEOUT])
        except OSError as e:
            return FormatAnalysis(None, None, [messages.INTERNAL_ERROR.format(err=e)])

        # Step 5
        if completed.returncode != 0:
            return _failure(schema_text, referenced, completed, tmp, config)

        # Step 6
        try:
            descriptor_set = descriptor_pb2.FileDescriptorSet.FromString(
                Path(tmp, _DESCRIPTOR_SET).read_bytes()
            )
        except (OSError, DecodeError) as e:
            return FormatAnalysis(None, None, [messages.INTERNAL_ERROR.format(err=e)])

    main = next((f for f in descriptor_set.file if f.name == _MAIN), None)
    if main is None:
        return FormatAnalysis(
            None, None, [messages.INTERNAL_ERROR.format(err=f"protoc output has no {_MAIN}")]
        )

    # Steps 7 to 9
    errors: list[str] = []
    if not main.package:
        errors.append(messages.PROTO_PACKAGE)
    if len(main.message_type) != 1:
        errors.append(messages.PROTO_MESSAGE_COUNT.format(n=len(main.message_type)))
    expected_name = None if errors else f"{main.package}.{main.message_type[0].name}"

    # Step 10
    return FormatAnalysis(expected_name, set(main.dependency) - config.builtin_proto_imports, errors)


def _is_safe_path(path: object) -> bool:
    """A relative path that stays inside the directory it is written to."""
    return (
        isinstance(path, str)
        and path != ""
        and not path.startswith("/")
        and not os.path.isabs(path)
        and "\\" not in path
        and "\0" not in path
        and ".." not in path.split("/")
    )


def _failure(
    schema_text: str,
    referenced: dict[str, str],
    completed: subprocess.CompletedProcess[str],
    tmp: str,
    config: ValidatorConfig,
) -> FormatAnalysis:
    """Result for a non-zero protoc exit. A missing import of the schema itself is left to stage D4.

    Any other failure means the text could not be analysed: external_refs is None, so
    D4/D5 are skipped (spec section 4, stage B).
    """
    imports = _imports(schema_text)
    lines = completed.stderr.splitlines()
    missing = {m.group(1) for line in lines if (m := _NOT_FOUND.fullmatch(line))}
    # Stage D4 only names the schema's own imports, so a file missing further down is reported here.
    if missing and missing <= imports and not missing & referenced.keys():
        return FormatAnalysis(None, imports - config.builtin_proto_imports, [])
    error = _clean_stderr(lines, tmp)
    if not error:
        # A crash or a signal: there is nothing to show as a schema error.
        return FormatAnalysis(None, None, [messages.INTERNAL_ERROR.format(
            err=f"protoc exited with code {completed.returncode} and no message")])
    return FormatAnalysis(None, None, [messages.PROTO_INVALID.format(err=error)])


def _imports(schema_text: str) -> set[str]:
    """The paths of the import statements outside comments."""
    code = _STRING_OR_COMMENT.sub(lambda m: m.group(0) if m.group(0)[0] in "\"'" else " ", schema_text)
    return {m.group(1) if m.group(1) is not None else m.group(2) for m in _IMPORT.finditer(code)}


def _clean_stderr(lines: list[str], tmp: str) -> str:
    """protoc's error lines without warnings, the temp directory or the main file's name."""
    # Longest first: on macOS one of the two spellings is a suffix of the other.
    temp_paths = sorted({tmp, os.path.realpath(tmp)}, key=len, reverse=True)
    kept: list[str] = []
    for line in lines:
        if "warning:" in line or line.startswith(("WARNING:", "W0000")):
            continue
        for temp_path in temp_paths:
            line = line.replace(temp_path + os.sep, "").replace(temp_path, "")
        line = line.removeprefix(_MAIN + ":").strip()
        if line:
            kept.append(line)
    shown = kept[:_PROTOC_ERRORS_SHOWN]
    if len(kept) > _PROTOC_ERRORS_SHOWN:
        shown.append(messages.PROTO_MORE_ERRORS.format(n=len(kept) - _PROTOC_ERRORS_SHOWN))
    return shorten("; ".join(shown), _PROTOC_MESSAGE_LIMIT)
