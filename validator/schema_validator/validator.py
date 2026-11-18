"""The validation pipeline (spec section 4): stages A to E, then the warnings."""
from __future__ import annotations

import contextvars
import re
import threading
from collections.abc import Callable
from typing import Any

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

# The registry's own not-found texts (class Errors in kafka-schema-registry 7.9.0), shown as they are.
# "Either the input schema or one its references is invalid" is not in 7.9 and is not shown.
_NOT_FOUND_PATTERNS = {
    40401: re.compile(r"Subject '[^'{}]{1,255}' not found\."),
    40402: re.compile(r"Version [0-9]{1,10} not found\."),
}


# The worker's stack: a default-depth schema needs more than a small default (128 KB on musl).
_WORKER_STACK_BYTES = 16 << 20
# threading.stack_size is process-global: set and restored under this lock.
_STACK_LOCK = threading.Lock()


def _start(worker: threading.Thread) -> None:
    """Starts the worker with a 16 MB stack, or the default one where the size cannot be set."""
    with _STACK_LOCK:
        try:
            old = threading.stack_size(_WORKER_STACK_BYTES)
        except Exception:  # not supported on this platform
            worker.start()
            return
        try:
            worker.start()
        finally:
            threading.stack_size(old)


class _Stoppable:
    """The registry client for one validation's worker thread.

    Once `stop` is set (the deadline passed), every further call raises
    SchemaRegistryUnavailable, so an abandoned worker finishes only the call in flight.
    """

    def __init__(self, sr: SchemaRegistryClient, stop: threading.Event) -> None:
        self._sr = sr
        self._stop = stop

    def __getattr__(self, name: str) -> Callable[..., Any]:
        method = getattr(self._sr, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            if self._stop.is_set():
                raise SchemaRegistryUnavailable("validation deadline passed")
            return method(*args, **kwargs)
        return call


def validate(request: ValidationRequest, sr: SchemaRegistryClient, config: ValidatorConfig) -> ValidationResult:
    """Never raises: an unexpected exception becomes a single INTERNAL_ERROR.

    The whole validation has one deadline, `total_timeout_seconds`. A worker that is still
    running at the deadline is abandoned: its result is discarded and it makes no further
    registry calls after the one in flight. The worker gets a 16 MB stack: the stack size is
    process-global, so it is set and restored around the start, under a lock.
    """
    outcome: list[ValidationResult | BaseException] = []
    stop = threading.Event()
    guarded = _Stoppable(sr, stop)  # type: ignore[arg-type]

    def work() -> None:
        try:
            outcome.append(_validate(request, guarded, config))  # type: ignore[arg-type]
        except BaseException as e:  # a thread must hand back whatever ended it
            outcome.append(e)

    try:
        # The worker runs in a copy of the caller's context, so a client that reads a ContextVar sees its value.
        context = contextvars.copy_context()
        worker = threading.Thread(target=context.run, args=(work,), name="schema-validator", daemon=True)
        _start(worker)
        worker.join(config.total_timeout_seconds)
        if worker.is_alive():
            stop.set()
            return build_result([messages.SR_UNREACHABLE], [])
        result = outcome[0]
        if isinstance(result, BaseException):
            return build_result([messages.INTERNAL_ERROR.format(err=_describe(result))], [])
        return result
    except Exception as e:
        stop.set()
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
    warnings: list[str] = list(analysis.warnings)
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
    if _is_known_message(text, code):
        return _rejected(text)
    # Anything else (no message, free text, a structure that may echo the schema or a header,
    # a code Schema Registry does not use) may come from a gateway or plugin: show only the code.
    return messages.SR_REJECTED_CODE.format(code=code)


def _is_known_message(text: str, code: object) -> bool:
    """True for the few Schema Registry 7.9 message texts that are safe and useful to show."""
    pattern = _NOT_FOUND_PATTERNS.get(code)
    return pattern is not None and pattern.fullmatch(text) is not None


def _rejected(reason: str) -> str:
    """E1 for a reason; "" (an "Invalid schema" message without one) gets the fixed text."""
    if not reason:
        return messages.SR_REJECTED_NO_REASON
    return messages.SR_REJECTED.format(reason=reason)


def _describe(err: BaseException) -> str:
    """str(err), or the exception's type name when str() itself fails."""
    try:
        return str(err)
    except Exception:
        return type(err).__name__
