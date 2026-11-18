"""Stage A (input sanity) and stage C (name rules) of the validation pipeline."""
from __future__ import annotations

from dataclasses import dataclass

from schema_validator import messages
from schema_validator.models import (
    COMPATIBILITY_LEVELS,
    SCHEMA_TYPES,
    ReferenceInput,
    ValidationRequest,
    ValidatorConfig,
)


@dataclass(frozen=True)
class InputCheck:
    errors: list[str]
    fatal: bool  # A1 or A2 failed: the pipeline returns immediately
    references_ok: bool  # False when A5 failed: the pipeline skips stage D


def check_input(request: ValidationRequest, config: ValidatorConfig) -> InputCheck:
    """Stage A. Runs every check; `fatal` tells the pipeline to stop after it."""
    errors: list[str] = []
    fatal = False

    # A1
    if request.schema_type not in SCHEMA_TYPES:
        errors.append(messages.UNSUPPORTED_FORMAT.format(x=request.schema_type))
        fatal = True

    # A2
    try:
        size = len(request.schema.encode("utf-8"))
    except UnicodeEncodeError:  # a lone surrogate
        errors.append(messages.SCHEMA_NOT_TEXT)
        fatal = True
    else:
        if not request.schema.strip():
            errors.append(messages.SCHEMA_EMPTY)
            fatal = True
        elif size > config.max_schema_bytes:
            errors.append(messages.SCHEMA_TOO_LARGE.format(n=size, max=config.max_schema_bytes))
            fatal = True

    # A3
    if request.compatibility not in COMPATIBILITY_LEVELS:
        errors.append(messages.UNKNOWN_COMPATIBILITY.format(x=request.compatibility))

    # A4
    if not _name_is_valid(request.name):
        errors.append(messages.NAME_INVALID)

    # A5
    reference_errors = _reference_errors(request.name, request.schema_type, request.references)
    errors.extend(reference_errors)

    return InputCheck(errors=errors, fatal=fatal, references_ok=not reference_errors)


def _name_is_valid(name: str) -> bool:
    return bool(name) and not any(c.isspace() for c in name) and not name.startswith(":")


def _effective_ref_string(schema_type: str, ref: ReferenceInput) -> str | None:
    """The ref_string that will be sent to the registry, or None if it is not compared.

    PROTOBUF derives it from schema_name, so the schema_name comparison already
    covers it. AVRO and JSON (and an unsupported type) use the given value; a missing
    one (None or "") is not compared. AVRO gets A5 for it, JSON gets D6 in stage D.
    """
    if schema_type == "PROTOBUF":
        return None
    return ref.ref_string or None


def _reference_errors(
    name: str, schema_type: str, references: list[ReferenceInput]
) -> list[str]:
    errors: list[str] = []
    seen_names: set[str] = set()
    seen_ref_strings: set[str] = set()
    # One message per repeated text, even if it repeats as both schema_name and ref_string.
    reported: set[str] = set()
    self_reference = False

    for ref in references:
        if ref.schema_name == name:
            self_reference = True
        for value, seen in (
            (ref.schema_name, seen_names),
            (_effective_ref_string(schema_type, ref), seen_ref_strings),
        ):
            if value is None:
                continue
            if value in seen and value not in reported:
                errors.append(messages.REFERENCE_DUPLICATE.format(x=value))
                reported.add(value)
            seen.add(value)

    errors.extend(
        messages.REF_VERSION_REQUIRED.format(s=ref.schema_name)
        for ref in references
        if not isinstance(ref.version, int) or isinstance(ref.version, bool) or ref.version < 1
    )

    if schema_type == "AVRO":
        errors.extend(
            messages.AVRO_REF_STRING.format(s=ref.schema_name)
            for ref in references
            if ref.ref_string != ref.schema_name
        )

    if self_reference:
        errors.append(messages.SELF_REFERENCE)
    return errors


def check_name(request: ValidationRequest, expected_name: str | None) -> list[str]:
    """Stage C. `expected_name` is None when stage B could not derive it (C3 skipped)."""
    previous_name = request.previous_name
    is_edit = previous_name is not None

    duplicate: list[str] = []
    # On an edit the schema's own stored name is not a clash (other case variants are).
    others = (n for n in request.existing_names if n != previous_name)
    if request.name.lower() in {n.lower() for n in others}:
        duplicate.append(messages.NAME_EXISTS.format(name=request.name))

    # The schema's own full name moved away from the stored name: one message
    # replaces C1 and C3.
    if is_edit and expected_name is not None and expected_name != previous_name:
        changed = messages.FULLNAME_CHANGED.format(
            previous_name=previous_name, expected_name=expected_name
        )
        return [changed, *duplicate]

    errors: list[str] = []
    if is_edit and request.name != previous_name:
        errors.append(messages.NAME_CHANGED.format(previous_name=previous_name))
    errors.extend(duplicate)
    if expected_name is not None and request.name != expected_name:
        errors.append(messages.NAME_MISMATCH.format(expected_name=expected_name))
    return errors
