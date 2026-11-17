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
    # An edit reads the latest version (one call): its format must be the same, the text must not be
    # an older version's, and the level rule below uses what the lookup found.
    level_warning = None
    unchanged_warning = None
    if request.previous_name is not None:
        try:
            latest = sr.get_latest(name)
            found = None
            if latest is not None:
                # The platform never sends a format change; this guards against a platform bug.
                if latest.schema_type != schema_type:
                    return build_result([messages.FORMAT_CHANGED.format(
                        name=name, registered=latest.schema_type, requested=schema_type)], [])
                found = sr.registered_version(name, request.schema, schema_type, resolution.resolved)
                if found is not None and found != latest.version:
                    # The registry would return the older version's id and register nothing.
                    return build_result([messages.SCHEMA_IS_OLDER_VERSION.format(
                        n=found, name=name, latest=latest.version)], [])
            # E6: the registry checks under the level in force, so a level change cannot come with a schema change.
            level = sr.get_compatibility_level(name)
            if level != request.compatibility:
                level_warning = messages.LEVEL_WARNING.format(
                    sr_level=level, compatibility=request.compatibility)
                if latest is not None:
                    # "Unchanged" also needs the same reference names on the same subjects at the same
                    # versions as the rows; the lookup uses the rows' references.
                    registered = [(r.name, r.subject, r.version) for r in latest.references]
                    requested = [(r.name, r.subject, r.version) for r in resolution.resolved]
                    if set(registered) == set(requested) and registered != requested:
                        # The registry's lookup is order-sensitive: a reordered save would register a new version.
                        return build_result([messages.LEVEL_REFERENCE_ORDER.format(
                            order=", ".join(f"{s} v{v}" for _, s, v in registered))], [])
                    if set(registered) != set(requested) or found is None:
                        return build_result([messages.LEVEL_AND_SCHEMA_CHANGED.format(
                            sr_level=level, compatibility=request.compatibility)], [])
            elif latest is not None and found == latest.version:
                unchanged_warning = messages.SCHEMA_UNCHANGED.format(n=latest.version, name=name)
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
        warnings.append(pinned_warning(resolution.resolved, resolution.latest))
    if level_warning is not None:
        warnings.append(level_warning)
    if unchanged_warning is not None:
        warnings.append(unchanged_warning)
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
    code = getattr(err, "code", None)
    if text and "{" not in text and _is_registry_code(code):
        return _rejected(text)
    # No message, one that embeds a structure (it may echo the schema or a header),
    # or a code Schema Registry does not use (the body may come from a proxy).
    return messages.SR_REJECTED_CODE.format(code=code)


def _is_registry_code(code: object) -> bool:
    """Schema Registry's own error codes: 409, or five digits from 40000 to 49999."""
    return code == 409 or (isinstance(code, int) and 40000 <= code <= 49999)


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
