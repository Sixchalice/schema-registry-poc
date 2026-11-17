"""Every user-facing message. Placeholders are str.format fields."""

# Stage A
UNSUPPORTED_FORMAT = "Unsupported schema format '{x}'. Use AVRO, JSON or PROTOBUF."
SCHEMA_EMPTY = "Schema is empty."
SCHEMA_NOT_TEXT = "Schema contains characters that are not valid text."
SCHEMA_TOO_LARGE = "Schema is too large ({n} bytes, limit {max})."
UNKNOWN_COMPATIBILITY = "Unknown compatibility level '{x}'."
NAME_INVALID = "Schema name is invalid."
REFERENCE_DUPLICATE = "Reference '{x}' is listed twice."
AVRO_REF_STRING = "For an Avro reference to '{s}', leave the reference text empty or write '{s}'."
SELF_REFERENCE = "A schema cannot reference itself."

# Stage C
NAME_CHANGED = (
    "A schema's name cannot change after creation (was '{previous_name}'). "
    "Create a new Schema instead."
)
NAME_EXISTS = "A Schema named '{name}' already exists (names are compared case-insensitively)."
NAME_MISMATCH = "The name must match the schema. It should be '{expected_name}'."
FULLNAME_CHANGED = (
    "The schema's full name changed from '{previous_name}' to '{expected_name}'. "
    "A schema's name cannot change; revert the namespace/name or create a new Schema."
)

# Stage D
REF_NOT_REGISTERED = "Referenced schema '{s}' is not registered in Schema Registry yet."
REF_WRONG_FORMAT = "'{s}' is a {their_type} schema; a {my_type} schema can only reference {my_type} schemas."
REF_CYCLE = "Reference cycle: '{s}' already depends on '{name}'."
REF_MISSING_ROW = "Your schema uses '{x}' but no reference is selected for it."
REF_UNUSED = "'{x}' is selected as a reference but is not used in the schema."
REF_UNUSED_PROTOBUF = (
    "'{s}' is selected as a reference but the schema does not import it. "
    "Write the import as: import \"{path}\";"
)
REF_STRING_REQUIRED = "Reference to '{s}' needs the text used in the schema (the $ref value)."
PINNED_WARNING = (
    "References will be pinned to: {list}. "
    "They stay on these versions until this schema is saved again."
)

# Stage E
SR_REJECTED = "Schema Registry rejected the schema: {reason}"
SR_REJECTED_NO_REASON = "Schema Registry rejected the schema without giving a reason."
SR_REJECTED_CODE = "Schema Registry rejected the schema (error code {code})."
SR_INCOMPATIBLE = "Not {sr_level}-compatible with version {n}: {descriptions}"
SR_MODE = ("Schema Registry does not accept new versions of '{name}' right now (mode {mode}). "
           "Contact the platform team.")
SR_UNREACHABLE = "Could not reach Schema Registry to validate. Try again."
LEVEL_AND_SCHEMA_CHANGED = (
    "The compatibility level and the schema cannot change in the same save. "
    "Schema Registry checks a new version under the level in force when it is registered "
    "(now '{sr_level}'). Save the new level '{compatibility}' with the schema unchanged first, "
    "then change the schema."
)
SR_UNAUTHORIZED = "The validator is not allowed to query Schema Registry. Contact the platform team."
LEVEL_WARNING = (
    "Compatibility was checked using the current level '{sr_level}', "
    "not the new level '{compatibility}'."
)

# Avro (5.1)
AVRO_TOP_LEVEL = "The top level of an Avro schema must be a single record, enum or fixed."
AVRO_NAMESPACE = "Avro schema needs a namespace, for example \"namespace\": \"com.team.domain\"."
AVRO_NESTED_NAMESPACE = (
    "Type '{name}' needs a namespace. Remove its \"namespace\" entry so it uses the schema's namespace."
)
AVRO_NESTED_NAME = (
    "Type name '{name}' has an empty part around a dot. "
    "Use a plain name, for example \"Address\", or a full name, for example \"com.team.domain.Address\"."
)
AVRO_NESTED_IN_REFERENCE = (
    "Type '{name}' is defined inside the Schema '{schema}'. A type nested in another Schema "
    "cannot be used here. Move it to its own Schema and reference that."
)
AVRO_INVALID = "Invalid Avro schema: {err}"

# JSON Schema (5.2)
JSON_TITLE_MISSING = "JSON schema needs a \"title\"; it becomes the schema name."
JSON_TITLE_FORMAT = (
    "The \"title\" must be a dotted name made of letters, digits and underscores, "
    "for example \"com.team.domain.Order\"."
)
JSON_TOP_LEVEL = "The top level of a JSON schema must have \"type\": \"object\"."
JSON_DRAFT = "Unsupported JSON Schema draft '{x}'."
JSON_INVALID = "Invalid JSON schema: {err}"
JSON_SCHEMA_MISSING = (
    'JSON schema needs a "$schema", for example "$schema": "http://json-schema.org/draft-07/schema#".'
)
JSON_NESTED_ID = (
    'Only the top level of a JSON schema may have "{kw}". Remove it from {paths}. '
    'A type that needs its own "{kw}" belongs in its own Schema, referenced with "$ref".'
)
JSON_NESTED_SCHEMA = (
    'Only the top level of a JSON schema may have "$schema". Remove it from {paths}. '
    'A JSON schema uses one draft.'
)
JSON_ROOT_ID_WITH_REF = (
    'In draft-07 and earlier, a "$ref" at the top level makes the "{kw}" next to it ignored. '
    'Move the "$ref" into "allOf".'
)
JSON_TOO_DEEP = "JSON schema is nested too deeply."
SCHEMA_TOO_DEEP = "Schema is nested more than {max} levels deep."

# Protobuf (5.3)
PROTO_IMPORT_PATH = "Invalid import path '{x}'."
PROTO_INVALID = "Invalid Protobuf schema: {err}"
PROTO_PACKAGE = "Protobuf schema needs a package, for example \"package com.team.domain;\"."
PROTO_MESSAGE_COUNT = (
    "A Protobuf schema must define exactly one top-level message (found {n}). "
    "Nest the others inside it or move them to their own Schema."
)

# Not given by the spec
NOT_JSON = "Schema is not valid JSON: {err}"
SR_INCOMPATIBLE_NO_VERSION = "Not {sr_level}-compatible with the existing versions: {descriptions}"
REF_VERSION_CONFLICT = (
    "'{s}' is used at more than one version: {uses}. A schema can use only one version of it. "
    "Save the Schemas that use an older version again so that they use v{latest}, "
    "then save this schema."
)
REF_NAME_CONFLICT = (
    "The reference name '{x}' is used for more than one Schema: {subjects}. "
    "Each Schema needs its own reference name."
)
REF_TOO_DEEP = "References of '{s}' are nested more than 20 levels deep."
PROTO_TIMEOUT = "Invalid Protobuf schema: the Protobuf compiler did not finish in 10 seconds."
INTERNAL_ERROR = "Internal validator error: {err}"
