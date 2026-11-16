"""Item 1: what does the compatibility endpoint do for a subject that does not exist yet?"""
import time

from sr import body, call, compat, q, register, show

RUN = str(int(time.time()))

GOOD = {
    "AVRO": {"type": "record", "name": "Order", "namespace": "p1.avro", "fields": [{"name": "id", "type": "string"}]},
    "JSON": {"title": "p1.json.Order", "type": "object", "properties": {"id": {"type": "string"}}},
    "PROTOBUF": 'syntax = "proto3";\npackage p1.proto;\nmessage Order { string id = 1; }\n',
}
BAD = {
    # syntactically JSON, semantically broken Avro (unknown type)
    "AVRO": {"type": "record", "name": "Order", "namespace": "p1.avro", "fields": [{"name": "id", "type": "strin"}]},
    "JSON": {"title": "p1.json.Order", "type": "objekt", "properties": {"id": {"type": 5}}},
    "PROTOBUF": 'syntax = "proto3";\npackage p1.proto;\nmessage Order { strin id = 1 }\n',
}
GARBAGE = {"AVRO": "{not json", "JSON": "{not json", "PROTOBUF": "this is not proto"}

print("== new subject: compatibility endpoint ==")
for t in GOOD:
    s = f"p1.new.{t}.{RUN}"
    show(f"{t} valid   /versions", compat(s, GOOD[t], t))
    show(f"{t} invalid /versions", compat(s, BAD[t], t))
    show(f"{t} garbage /versions", compat(s, GARBAGE[t], t))
    show(f"{t} invalid /versions/latest", compat(s, BAD[t], t, version="latest"))

print("\n== new subject: other endpoints that might validate without registering ==")
for t in GOOD:
    s = f"p1.new.{t}.{RUN}"
    show(f"{t} invalid POST /subjects/S (lookup)", call("POST", f"/subjects/{q(s)}", body(BAD[t], t)))
    show(f"{t} valid   POST /subjects/S (lookup)", call("POST", f"/subjects/{q(s)}", body(GOOD[t], t)))

print("\n== existing subject: compatibility endpoint ==")
for t in GOOD:
    s = f"p1.old.{t}.{RUN}"
    show(f"{t} register v1", register(s, GOOD[t], t))
    show(f"{t} valid   /versions", compat(s, GOOD[t], t))
    show(f"{t} invalid /versions", compat(s, BAD[t], t))
    show(f"{t} garbage /versions", compat(s, GARBAGE[t], t))

print("\n== registering the invalid ones (what America would see at registration) ==")
for t in GOOD:
    s = f"p1.reg.{t}.{RUN}"
    show(f"{t} invalid register", register(s, BAD[t], t))
    show(f"{t} garbage register", register(s, GARBAGE[t], t))

print("\n== references in the compatibility call ==")
s = f"p1.refs.{RUN}"
ref_missing = [{"name": "p1.avro.Nope", "subject": f"p1.does.not.exist.{RUN}", "version": 1}]
uses_ref = {"type": "record", "name": "UsesRef", "namespace": "p1.avro", "fields": [{"name": "x", "type": "p1.avro.Nope"}]}
show("new subject, reference to missing subject", compat(s, uses_ref, "AVRO", ref_missing))
show("new subject, uses undefined type, no refs", compat(s, uses_ref, "AVRO"))
show("register with reference to missing subject", register(s, uses_ref, "AVRO", ref_missing))

print("\n== config endpoint ==")
show("GET /config", call("GET", "/config"))
show("GET /config/<missing>", call("GET", f"/config/{q('p1.none.' + RUN)}"))
show("GET /config/<missing>?defaultToGlobal=true", call("GET", f"/config/{q('p1.none.' + RUN)}?defaultToGlobal=true"))
s = f"p1.old.AVRO.{RUN}"
show("GET /config/<existing, no own level>", call("GET", f"/config/{q(s)}"))
show("GET /config/<existing>?defaultToGlobal=true", call("GET", f"/config/{q(s)}?defaultToGlobal=true"))
show("PUT /config/<existing> FULL", call("PUT", f"/config/{q(s)}", {"compatibility": "FULL"}))
show("GET /config/<existing>", call("GET", f"/config/{q(s)}"))
