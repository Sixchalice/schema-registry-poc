"""Items 3, 4, 5 plus edge cases: references per format, JSON drafts, built-in proto imports,
cycles, soft delete, subject-name rules, shape of real incompatibility messages."""
import json
import time

from sr import call, compat, q, register, show

RUN = str(int(time.time()))
N = f"p2r{RUN}"  # namespace unique per run


def ref(name, subject, version=1):
    return [{"name": name, "subject": subject, "version": version}]


print("== AVRO references ==")
addr = {"type": "record", "name": "Address", "namespace": N, "fields": [{"name": "city", "type": "string"}]}
cust = {"type": "record", "name": "Customer", "namespace": N, "fields": [{"name": "addr", "type": f"{N}.Address"}]}
cust_unq = {"type": "record", "name": "Customer2", "namespace": N, "fields": [{"name": "addr", "type": "Address"}]}
show("register Address", register(f"{N}.Address", addr))
show("Customer, ref name = full name", register(f"{N}.Customer", cust, references=ref(f"{N}.Address", f"{N}.Address")))
show("Customer, ref name = arbitrary text", register(f"{N}.CustomerB", cust, references=ref("whatever", f"{N}.Address")))
show("Customer, no reference row", register(f"{N}.CustomerC", cust))
show("Customer2, unqualified use + ref", register(f"{N}.Customer2", cust_unq, references=ref(f"{N}.Address", f"{N}.Address")))
show("Customer, unused extra ref is accepted?", register(f"{N}.CustomerD", addr | {"name": "CustomerD"}, references=ref(f"{N}.Address", f"{N}.Address")))
show("ref version -1 (latest) accepted?", register(f"{N}.CustomerE", cust | {"name": "CustomerE"}, references=ref(f"{N}.Address", f"{N}.Address", -1)))
show("  stored as", call("GET", f"/subjects/{q(N + '.CustomerE')}/versions/latest"))
show("ref version 99 (missing version)", compat(f"{N}.CustomerF", cust | {"name": "CustomerF"}, references=ref(f"{N}.Address", f"{N}.Address", 99)))

print("\n== JSON references ==")
jaddr = {"title": f"{N}.JAddress", "type": "object", "properties": {"city": {"type": "string"}},
         "definitions": {"Zip": {"type": "string"}}}
show("register JAddress", register(f"{N}.JAddress", jaddr, "JSON"))


def jorder(title, refval):
    return {"title": f"{N}.{title}", "type": "object", "properties": {"a": {"$ref": refval}}}


cases = [
    ("$ref=X            ref name=X", "address.json", "address.json"),
    ("$ref=X#/defs/Zip  ref name=X", "address.json#/definitions/Zip", "address.json"),
    ("$ref=X#/defs/Zip  ref name=X#/defs/Zip", "address.json#/definitions/Zip", "address.json#/definitions/Zip"),
    ("$ref=X#           ref name=X", "address.json#", "address.json"),
    ("$ref=subject name ref name=same", f"{N}.JAddress", f"{N}.JAddress"),
    ("$ref=X            ref name=other", "address.json", "other.json"),
    ("$ref=https URL    ref name=same URL", "https://example.com/schemas/address.json", "https://example.com/schemas/address.json"),
]
for i, (label, refval, refname) in enumerate(cases):
    show(label, compat(f"{N}.JOrder{i}", jorder(f"JOrder{i}", refval), "JSON", ref(refname, f"{N}.JAddress")))
show("$ref=X, no reference row", compat(f"{N}.JOrderN", jorder("JOrderN", "address.json"), "JSON"))
show("$ref=X, no reference row (register)", register(f"{N}.JOrderN", jorder("JOrderN", "address.json"), "JSON"))
show("unused reference row accepted?", compat(f"{N}.JOrderU", {"title": f"{N}.JOrderU", "type": "object"}, "JSON", ref("address.json", f"{N}.JAddress")))
show("internal #/definitions ref only", compat(f"{N}.JOrderI", {"title": f"{N}.JOrderI", "type": "object", "properties": {"z": {"$ref": "#/definitions/Z"}}, "definitions": {"Z": {"type": "string"}}}, "JSON"))
show("internal ref to missing definition", compat(f"{N}.JOrderM", {"title": f"{N}.JOrderM", "type": "object", "properties": {"z": {"$ref": "#/definitions/Nope"}}}, "JSON"))

print("\n== PROTOBUF references ==")
paddr = f'syntax = "proto3";\npackage {N};\nmessage PAddress {{ string city = 1; }}\n'
show("register PAddress", register(f"{N}.PAddress", paddr, "PROTOBUF"))


def porder(name, imp):
    return f'syntax = "proto3";\npackage {N};\nimport "{imp}";\nmessage {name} {{ {N}.PAddress a = 1; }}\n'


show("import P, ref name=P", compat(f"{N}.POrder1", porder("POrder1", "acme/address.proto"), "PROTOBUF", ref("acme/address.proto", f"{N}.PAddress")))
show("import P, ref name=other", compat(f"{N}.POrder2", porder("POrder2", "acme/address.proto"), "PROTOBUF", ref("acme/other.proto", f"{N}.PAddress")))
show("import P, no reference row", compat(f"{N}.POrder3", porder("POrder3", "acme/address.proto"), "PROTOBUF"))
show("import = subject name (no .proto)", compat(f"{N}.POrder4", porder("POrder4", f"{N}.PAddress"), "PROTOBUF", ref(f"{N}.PAddress", f"{N}.PAddress")))
show("unused reference row accepted?", compat(f"{N}.POrder5", f'syntax = "proto3";\npackage {N};\nmessage POrder5 {{ string a = 1; }}\n', "PROTOBUF", ref("acme/address.proto", f"{N}.PAddress")))
show("uses type without import or ref", compat(f"{N}.POrder6", f'syntax = "proto3";\npackage {N};\nmessage POrder6 {{ {N}.PAddress a = 1; }}\n', "PROTOBUF"))

print("\n== cross-format reference ==")
show("AVRO referencing a JSON subject", compat(f"{N}.X1", cust | {"name": "X1"}, "AVRO", ref(f"{N}.Address", f"{N}.JAddress")))
show("PROTOBUF referencing an AVRO subject", compat(f"{N}.X2", porder("X2", "acme/address.proto"), "PROTOBUF", ref("acme/address.proto", f"{N}.Address")))

print("\n== built-in PROTOBUF imports, no reference rows ==")
imports = [
    ("google/protobuf/timestamp.proto", "google.protobuf.Timestamp"),
    ("google/protobuf/wrappers.proto", "google.protobuf.StringValue"),
    ("google/protobuf/any.proto", "google.protobuf.Any"),
    ("google/protobuf/struct.proto", "google.protobuf.Struct"),
    ("google/protobuf/duration.proto", "google.protobuf.Duration"),
    ("google/protobuf/empty.proto", "google.protobuf.Empty"),
    ("google/protobuf/field_mask.proto", "google.protobuf.FieldMask"),
    ("google/protobuf/descriptor.proto", "google.protobuf.FileOptions"),
    ("google/protobuf/api.proto", "google.protobuf.Api"),
    ("google/protobuf/type.proto", "google.protobuf.Type"),
    ("google/protobuf/source_context.proto", "google.protobuf.SourceContext"),
    ("google/type/date.proto", "google.type.Date"),
    ("google/type/money.proto", "google.type.Money"),
    ("google/type/latlng.proto", "google.type.LatLng"),
    ("google/type/decimal.proto", "google.type.Decimal"),
    ("google/type/timeofday.proto", "google.type.TimeOfDay"),
    ("google/type/datetime.proto", "google.type.DateTime"),
    ("google/type/postal_address.proto", "google.type.PostalAddress"),
    ("google/type/color.proto", "google.type.Color"),
    ("google/type/calendar_period.proto", "google.type.CalendarPeriod"),
    ("google/type/dayofweek.proto", "google.type.DayOfWeek"),
    ("google/type/expr.proto", "google.type.Expr"),
    ("google/type/fraction.proto", "google.type.Fraction"),
    ("google/type/interval.proto", "google.type.Interval"),
    ("google/type/localized_text.proto", "google.type.LocalizedText"),
    ("google/type/month.proto", "google.type.Month"),
    ("google/type/phone_number.proto", "google.type.PhoneNumber"),
    ("google/type/quaternion.proto", "google.type.Quaternion"),
    ("confluent/meta.proto", "confluent.Meta"),
    ("confluent/type/decimal.proto", "confluent.type.Decimal"),
    ("google/rpc/status.proto", "google.rpc.Status"),
    ("google/api/annotations.proto", None),
    ("buf/validate/validate.proto", None),
]
for i, (imp, typ) in enumerate(imports):
    field = f"{typ} f = 1;" if typ else "string f = 1;"
    text = f'syntax = "proto3";\npackage {N};\nimport "{imp}";\nmessage B{i} {{ {field} }}\n'
    status, payload = register(f"{N}.B{i}", text, "PROTOBUF")
    verdict = "ACCEPTED" if status == 200 else "REJECTED " + json.dumps(payload)[-150:]
    print(f"  {imp:<40} {verdict}")

print("\n== JSON Schema drafts ==")
drafts = [
    "http://json-schema.org/draft-03/schema#",
    "http://json-schema.org/draft-04/schema#",
    "http://json-schema.org/draft-06/schema#",
    "http://json-schema.org/draft-07/schema#",
    "http://json-schema.org/draft-07/schema",
    "https://json-schema.org/draft/2019-09/schema",
    "https://json-schema.org/draft/2020-12/schema",
    "https://json-schema.org/draft/next/schema",
    "http://example.com/not-a-draft",
]
for i, d in enumerate(drafts):
    s = {"$schema": d, "title": f"{N}.D{i}", "type": "object", "properties": {"id": {"type": "string"}}}
    c = compat(f"{N}.D{i}", s, "JSON")
    r = register(f"{N}.D{i}", s, "JSON")
    print(f"  {d:<48} compat={c[0]} {json.dumps(c[1])[-90:]}  register={r[0]} {json.dumps(r[1])[-70:]}")
show("no $schema at all", register(f"{N}.DN", {"title": f"{N}.DN", "type": "object"}, "JSON"))
# does SR really validate against the meta-schema of the declared draft?
show("draft-07, properties is a string", compat(f"{N}.DB1", {"$schema": "http://json-schema.org/draft-07/schema#", "title": f"{N}.DB1", "type": "object", "properties": "nope"}, "JSON"))
show("2020-12, type is a number", compat(f"{N}.DB2", {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": f"{N}.DB2", "type": 5}, "JSON"))
show("2020-12 with $defs + internal $ref", compat(f"{N}.DB3", {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": f"{N}.DB3", "type": "object", "properties": {"z": {"$ref": "#/$defs/Z"}}, "$defs": {"Z": {"type": "string"}}}, "JSON"))
show("top-level javaType property", compat(f"{N}.DB4", {"title": f"{N}.DB4", "type": "object", "javaType": "com.acme.Order"}, "JSON"))

print("\n== shape of a real incompatibility (verbose) ==")
v2_bad = {"type": "record", "name": "Address", "namespace": N, "fields": [{"name": "city", "type": "string"}, {"name": "zip", "type": "string"}]}
show("AVRO add required field (BACKWARD)", compat(f"{N}.Address", v2_bad))
show("JSON incompatible", compat(f"{N}.JAddress", {"title": f"{N}.JAddress", "type": "object", "properties": {"city": {"type": "integer"}}}, "JSON"))
show("PROTO incompatible", compat(f"{N}.PAddress", f'syntax = "proto3";\npackage {N};\nmessage PAddress {{ int32 city = 1; }}\n', "PROTOBUF"))
show("change format on existing subject", compat(f"{N}.Address", jaddr, "JSON"))

print("\n== cycle: can an existing subject start referencing something that references it? ==")
a2 = {"type": "record", "name": "Address", "namespace": N, "fields": [{"name": "city", "type": "string"}, {"name": "c", "type": ["null", f"{N}.Customer"], "default": None}]}
show("Address v2 referencing Customer (compat)", compat(f"{N}.Address", a2, references=ref(f"{N}.Customer", f"{N}.Customer")))
show("Address v2 referencing Customer (register)", register(f"{N}.Address", a2, references=ref(f"{N}.Customer", f"{N}.Customer")))

print("\n== delete behaviour ==")
show("soft-delete referenced Address", call("DELETE", f"/subjects/{q(N + '.Address')}"))
show("referencedby", call("GET", f"/subjects/{q(N + '.Address')}/versions/1/referencedby"))
solo = {"type": "record", "name": "Solo", "namespace": N, "fields": [{"name": "a", "type": "string"}]}
show("register Solo", register(f"{N}.Solo", solo))
show("soft-delete Solo", call("DELETE", f"/subjects/{q(N + '.Solo')}"))
show("GET latest after soft delete", call("GET", f"/subjects/{q(N + '.Solo')}/versions/latest"))
solo_incompat = solo | {"fields": [{"name": "a", "type": "string"}, {"name": "b", "type": "string"}]}
show("compat incompatible vs soft-deleted", compat(f"{N}.Solo", solo_incompat))
show("register incompatible after soft delete", register(f"{N}.Solo", solo_incompat))
show("versions now", call("GET", f"/subjects/{q(N + '.Solo')}/versions"))

print("\n== subject names ==")
for name in [f"{N}.Case", f"{N}.case", f"{N}.with space", f"{N}.with/slash", f":.ctx:{N}.InCtx", f"{N}.with:colon", f"{N}.üñí", f"__{N}", f"{N}.x" + "y" * 300]:
    r = register(name, {"type": "record", "name": "S", "namespace": N, "fields": []})
    print(f"  {name[:50]!r:<56} -> {r[0]} {json.dumps(r[1])[:110]}")
subs = call("GET", "/subjects")[1]
print("  listed:", [s[:40] for s in subs if s.startswith(N + ".") and ("ase" in s or "with" in s or "ctx" in s)], [s for s in subs if "InCtx" in s])
