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
REF_WRONG_FORMAT = (
    "'{s}' is a schema in {their_type} format. "
    "A schema in {my_type} format can only reference schemas in {my_type} format."
)
REF_CYCLE = "Reference cycle: '{s}' already depends on '{name}'."
REF_MISSING_ROW = "Your schema uses '{x}' but no reference is selected for it."
REF_UNUSED = "'{x}' is selected as a reference but is not used in the schema."
REF_UNUSED_PROTOBUF = (
    "'{s}' is selected as a reference but the schema does not import it. "
    "Write the import as: import \"{path}\";"
)
REF_STRING_REQUIRED = "Reference to '{s}' needs the text used in the schema (the $ref value)."
REF_VERSION_REQUIRED = "Select a version for reference '{s}'."
REF_VERSION_NOT_FOUND = "'{s}' has no version {v} (the latest is {latest})."
PINNED_WARNING = "References will be pinned to: {list}."
PINNED_WARNING_NEWER = (
    "References will be pinned to: {list}. To update one, select a newer version for it."
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
    "(now '{sr_level}'). Save the new level '{compatibility}' with the schema and its reference "
    "versions unchanged first, then change the schema."
)
LEVEL_REFERENCE_ORDER = (
    "The references are listed in a different order than in the registered version ({order}). "
    "Keep that order, so that only the compatibility level changes."
)
FORMAT_CHANGED = (
    "'{name}' is registered as {registered}. Its format cannot change: "
    "to use {requested}, create a new Schema."
)
SCHEMA_IS_OLDER_VERSION = (
    "This schema is identical to version {n} of '{name}', and the latest is version {latest}. "
    "Schema Registry would not register it again, so producers would keep using version {latest}. "
    "Change the schema so that it differs from every registered version."
)
SCHEMA_UNCHANGED = "This is already version {n} of '{name}'. Saving it changes nothing in Schema Registry."
SR_UNAUTHORIZED = "The validator is not allowed to query Schema Registry. Contact the platform team."
LEVEL_WARNING = (
    "Compatibility was checked using the current level '{sr_level}', "
    "not the new level '{compatibility}'."
)

# Avro (5.1)
AVRO_TOP_LEVEL = "The top level of an Avro schema must be a single record, enum or fixed."
AVRO_NOT_A_MESSAGE = (
    "This Schema is an Avro {kind}. A producer cannot send it as a message, because under RecordNameStrategy "
    "a message must be a record. Use it as a reference from a record Schema."
)
AVRO_NAMESPACE = "Avro schema needs a namespace, for example \"namespace\": \"com.team.domain\"."
AVRO_NESTED_NAMESPACE = (
    "Type '{name}' needs a namespace. Remove its \"namespace\" entry so it uses the schema's namespace."
)
AVRO_NESTED_NAME = (
    "Type name '{name}' has an empty part around a dot. "
    "Use a plain name, for example \"Address\", or a full name, for example \"com.team.domain.Address\"."
)
AVRO_NAME_CHARS = (
    "'{name}' is not a valid Avro name. Each part between dots must start with a letter (A-Z, a-z) "
    "or an underscore and contain only letters, digits and underscores."
)
AVRO_NESTED_IN_REFERENCE = (
    "Type '{name}' is defined inside the Schema '{schema}'. A type nested in another Schema "
    "cannot be used here. Move it to its own Schema and reference that."
)
AVRO_INVALID = "Invalid Avro schema: {err}"
AVRO_WRONG_KIND = "an entry has the wrong kind of value, for example an object where a type name is expected"
AVRO_UNION_DEFAULT = (
    "The default of field '{field}' in '{record}' must be a value of the first type in its union ({first}). "
    "Put the type of the default first in the union."
)
AVRO_DEFAULT_INVALID = "The default of field '{field}' in '{record}' is not a valid {type} value."

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
JSON_ROOT_ID_FRAGMENT = (
    'The top-level "{kw}" cannot have a fragment. Remove the "#..." part from \'{value}\'.'
)
JSON_REF_SELF_URL = (
    'A "$ref" to a place in this schema must start with "#", for example "#/definitions/Item". '
    'Schema Registry rejects one written with the schema\'s own "{kw}". Change it at {paths}.'
)
JSON_REF_NOT_FOUND = 'A "$ref" points to nothing in this schema. Check the part after "#" at {paths}.'
JSON_REF_CLIENTS_DIFFER = (
    'The Python client cannot follow a "$ref", or follows it to a different place than Schema Registry. '
    'Write it as a plain pointer, for example "#/definitions/Item", with no "+", trailing "/" or anchor '
    'that is not defined, at {paths}.'
)
JSON_FORMAT_NOT_CHECKED = (
    'This schema uses "format" at {paths}. Java producers can reject values that do not match it, '
    'while Python producers do not check it, so the two can accept different data.'
)
JSON_TOO_DEEP = "JSON schema is nested too deeply."
SCHEMA_TOO_DEEP = "Schema is nested more than {max} levels deep."

# Protobuf (5.3)
PROTO_IMPORT_PATH = "Invalid import path '{x}'."
PROTO_INVALID = "Invalid Protobuf schema: {err}"
PROTO_MORE_ERRORS = "and {n} more"
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
    "Update the Schemas that use an older version to v{latest}: select v{latest} for '{s}' "
    "in their references and save them. Then save this schema."
)
REF_VERSION_CONFLICT_DIRECT = (
    "'{s}' is used at more than one version: {uses}. A schema can use only one version of it. "
    "Select the same version for '{s}' as the Schemas it comes through, or update those "
    "Schemas to v{chosen}: select v{chosen} for '{s}' in their references and save them."
)
REF_VERSION_CONFLICT_DIRECT_OLDER = (
    "'{s}' is used at more than one version: {uses}. A schema can use only one version of it. "
    "Select v{latest} for '{s}', the version the Schemas it comes through use."
)
REF_NAME_CONFLICT = (
    "The reference name '{x}' is used for more than one Schema: {subjects}. "
    "Each Schema needs its own reference name."
)
REF_TOO_DEEP = "References of '{s}' are nested more than {max} levels deep."
PROTO_TIMEOUT = "Invalid Protobuf schema: the Protobuf compiler did not finish in {seconds:g} seconds."
INTERNAL_ERROR = "Internal validator error: {err}"
