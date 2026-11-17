"""The validation pipeline (spec section 4): stages A to E, then the warnings."""
from __future__ import annotations

from schema_validator import messages
from schema_validator.formats import ANALYSERS
from schema_validator.models import ReferenceInput, ValidationRequest, ValidationResult, ValidatorConfig
from schema_validator.references import (
    Resolution, cross_check, normalise_rows, pinned_warning, resolve, through_rows,
)
from schema_validator.result import build_result
from schema_validator.rules import check_input, check_name
from schema_validator.sr_client import (
    CompatibilityVerdict,
    SchemaRegistryClient,
    SchemaRegistryRejected,
    SchemaRegistryUnauthorized,
    SchemaRegistryUnavailable,
)
from schema_validator.sr_messages import invalid_schema_reason, parse_incompatibility

# The registry failures that become a stage E error. Anything else is an internal error.
_SR_FAILURES = (SchemaRegistryUnavailable, SchemaRegistryUnauthorized, SchemaRegistryRejected)


def validate(request: ValidationRequest, sr: SchemaRegistryClient, config: ValidatorConfig) -> ValidationResult:
    """Never raises: an unexpected exception becomes a single INTERNAL_ERROR."""
    try:
        return _validate(request, sr, config)
    except Exception as e:
        return build_result([messages.INTERNAL_ERROR.format(err=_describe(e))], [])


def _validate(request: ValidationRequest, sr: SchemaRegistryClient, config: ValidatorConfig) -> ValidationResult:
    # Stage A
    check = check_input(request, config)
    a = list(check.errors)
    if check.fatal:
        return build_result(a, [])

    name = request.name
    schema_type = request.schema_type
    d: list[str] = []
    e: list[str] = []
    rows: list[ReferenceInput] = []
    resolution = Resolution(resolved=[], referenced={}, errors=[])

    # Stage D, first part: the rows and their resolution in Schema Registry.
    if check.references_ok:
        rows, d6 = normalise_rows(schema_type, request.references)
        try:
            resolution = resolve(name, schema_type, rows, sr)
        except _SR_FAILURES as err:
            e.append(_sr_failure(err))
        d.extend(resolution.errors)
        d.extend(d6)

    # Stage B
    analysis = ANALYSERS[schema_type](request.schema, resolution.referenced, config)
    b = list(analysis.errors)

    # Stage C
    c = check_name(request, analysis.expected_name)

    # Stage D, second part: rows against the names the text uses.
    if check.references_ok and analysis.external_refs is not None:
        external_refs = analysis.external_refs
        if schema_type == "AVRO":
            external_refs = through_rows(rows, external_refs, resolution.provided_by)
        d.extend(cross_check(schema_type, rows, external_refs))

    errors = a + b + c + d
    if errors or e:
        return build_result(errors + e, [])

    # Stage B found no error but could not analyse the schema: never pass it unchecked.
    if analysis.expected_name is None or analysis.external_refs is None:
        return build_result([messages.INTERNAL_ERROR.format(
            err=f"the {schema_type} analyser gave no name or references and no error")], [])

    # Stage E
    # E6: the registry checks under the level in force, so a level change cannot come with a schema change.
    level_warning = None
    if request.previous_name is not None:
        try:
            level = sr.get_compatibility_level(name)
            if level != request.compatibility:
                level_warning = messages.LEVEL_WARNING.format(
                    sr_level=level, compatibility=request.compatibility)
                latest = sr.get_latest(name)
                if latest is not None and not sr.is_registered(
                        name, request.schema, schema_type, latest.references):
                    return build_result([messages.LEVEL_AND_SCHEMA_CHANGED.format(
                        sr_level=level, compatibility=request.compatibility)], [])
        except _SR_FAILURES as err:
            return build_result([_sr_failure(err)], [])
    try:
        verdict = sr.check_compatibility(name, request.schema, schema_type, tuple(resolution.resolved))
    except _SR_FAILURES as err:
        return build_result([_sr_failure(err)], [])
    e.extend(_verdict_errors(verdict, request.compatibility))
    if e:
        return build_result(e, [])

    # E5: the subject (or the registry) must accept new versions.
    try:
        mode = sr.get_mode(name)
    except _SR_FAILURES as err:
        return build_result([_sr_failure(err)], [])
    if mode != "READWRITE":
        return build_result([messages.SR_MODE.format(name=name, mode=mode)], [])

    # Warnings
    warnings: list[str] = []
    if resolution.resolved:
        warnings.append(pinned_warning(resolution.resolved))
    if level_warning is not None:
        warnings.append(level_warning)
    return build_result([], warnings)


def _verdict_errors(verdict: CompatibilityVerdict, compatibility: str) -> list[str]:
    """E1 or E2 for a verdict; nothing when it is compatible."""
    if verdict.is_compatible:
        return []
    for message in verdict.messages:
        reason = invalid_schema_reason(message)
        if reason is not None:
            return [_rejected(reason)]
    incompatibility = parse_incompatibility(verdict.messages)
    sr_level = incompatibility.level or compatibility
    descriptions = "; ".join(incompatibility.descriptions)
    if incompatibility.old_version is None:
        text = messages.SR_INCOMPATIBLE_NO_VERSION.format(sr_level=sr_level, descriptions=descriptions)
    else:
        text = messages.SR_INCOMPATIBLE.format(
            sr_level=sr_level, n=incompatibility.old_version, descriptions=descriptions)
    if not descriptions:
        # Only version and level entries: drop the separator that introduced the descriptions.
        text = text.rstrip().removesuffix(":")
    return [text]


def _sr_failure(err: Exception) -> str:
    """The stage E error for a registry failure in any step (E1, E3 or E4)."""
    if isinstance(err, SchemaRegistryUnavailable):
        return messages.SR_UNREACHABLE
    if isinstance(err, SchemaRegistryUnauthorized):
        return messages.SR_UNAUTHORIZED
    text = str(err)
    reason = invalid_schema_reason(text)
    if reason is not None:
        return _rejected(reason)
    if text and "{" not in text:
        return _rejected(text)
    # No message, or one that embeds a structure (it may echo the schema or a header).
    code = getattr(err, "code", None)
    return messages.SR_REJECTED_CODE.format(code=code)


def _rejected(reason: str) -> str:
    """E1 for a reason; "" (an "Invalid schema" message without one) gets the fixed text."""
    if not reason:
        return messages.SR_REJECTED_NO_REASON
    return messages.SR_REJECTED.format(reason=reason)


def _describe(err: Exception) -> str:
    """str(err), or the exception's type name when str() itself fails."""
    try:
        return str(err)
    except Exception:
        return type(err).__name__
