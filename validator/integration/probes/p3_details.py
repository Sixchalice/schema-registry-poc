"""Full text of messages that were cut in p2, plus cycles for JSON and PROTOBUF."""
import time
from sr import compat, register, call, q

RUN = str(int(time.time())); N = f"p3r{RUN}"
def ref(name, subject, version=1): return [{"name": name, "subject": subject, "version": version}]
def details(res):
    st, p = res
    msgs = p.get("messages") or [p.get("message", "")] if isinstance(p, dict) else [str(p)]
    out = []
    for m in msgs:
        out.append(m.split("details: ", 1)[1] if "details: " in m else m)
    return f"{st} is_compatible={p.get('is_compatible') if isinstance(p, dict) else None} :: " + " || ".join(out)

addr = {"type": "record", "name": "Address", "namespace": N, "fields": [{"name": "city", "type": "string"}]}
cust = {"type": "record", "name": "Customer", "namespace": N, "fields": [{"name": "addr", "type": f"{N}.Address"}]}
register(f"{N}.Address", addr); register(f"{N}.Customer", cust, references=ref(f"{N}.Address", f"{N}.Address"))
jaddr = {"title": f"{N}.JAddress", "type": "object", "properties": {"city": {"type": "string"}}}
register(f"{N}.JAddress", jaddr, "JSON")
jcust = {"title": f"{N}.JCustomer", "type": "object", "properties": {"a": {"$ref": "address.json"}}}
print("JCustomer register", register(f"{N}.JCustomer", jcust, "JSON", ref("address.json", f"{N}.JAddress")))
paddr = f'syntax = "proto3";\npackage {N};\nmessage PAddress {{ string city = 1; }}\n'
register(f"{N}.PAddress", paddr, "PROTOBUF")
pcust = f'syntax = "proto3";\npackage {N};\nimport "a/address.proto";\nmessage PCustomer {{ PAddress a = 1; }}\n'
print("PCustomer register", register(f"{N}.PCustomer", pcust, "PROTOBUF", ref("a/address.proto", f"{N}.PAddress")))

print("\nmissing ref subject   :", details(compat(f"{N}.M", cust | {"name": "M"}, references=ref(f"{N}.Address", f"{N}.nope"))))
print("missing ref version   :", details(compat(f"{N}.M", cust | {"name": "M"}, references=ref(f"{N}.Address", f"{N}.Address", 99))))
print("AVRO -> JSON subject  :", details(compat(f"{N}.X1", cust | {"name": "X1"}, references=ref(f"{N}.Address", f"{N}.JAddress"))))
print("PROTO -> AVRO subject :", details(compat(f"{N}.X2", pcust.replace("PCustomer", "X2"), "PROTOBUF", ref("a/address.proto", f"{N}.Address"))))
print("JSON -> AVRO subject  :", details(compat(f"{N}.X3", jcust | {"title": f"{N}.X3"}, "JSON", ref("address.json", f"{N}.Address"))))

print("\n-- cycles (edit an existing subject so it references something that references it) --")
a2 = {"type": "record", "name": "Address", "namespace": N, "fields": [{"name": "city", "type": "string"}, {"name": "c", "type": ["null", f"{N}.Customer"], "default": None}]}
print("AVRO cycle  :", details(compat(f"{N}.Address", a2, references=ref(f"{N}.Customer", f"{N}.Customer"))))
ja2 = {"title": f"{N}.JAddress", "type": "object", "properties": {"city": {"type": "string"}, "c": {"$ref": "customer.json"}}}
print("JSON cycle  :", details(compat(f"{N}.JAddress", ja2, "JSON", ref("customer.json", f"{N}.JCustomer"))))
print("JSON cycle register:", register(f"{N}.JAddress", ja2, "JSON", ref("customer.json", f"{N}.JCustomer")))
pa2 = f'syntax = "proto3";\npackage {N};\nimport "a/customer.proto";\nmessage PAddress {{ string city = 1; PCustomer c = 2; }}\n'
print("PROTO cycle :", details(compat(f"{N}.PAddress", pa2, "PROTOBUF", ref("a/customer.proto", f"{N}.PCustomer"))))
print("PROTO cycle register:", register(f"{N}.PAddress", pa2, "PROTOBUF", ref("a/customer.proto", f"{N}.PCustomer")))

print("\n-- full incompatibility message list --")
v2 = addr | {"fields": addr["fields"] + [{"name": "zip", "type": "string"}]}
for m in compat(f"{N}.Address", v2)[1]["messages"]: print("   ", m)
print("\n-- PROTOBUF: two top-level messages, how SR stores it; normalised text --")
two = f'syntax = "proto3";\npackage {N};\nmessage A {{ string x = 1; }}\nmessage B {{ string y = 1; }}\n'
print(register(f"{N}.A", two, "PROTOBUF")); print(repr(call("GET", f"/subjects/{q(N + '.A')}/versions/latest")[1]["schema"]))
print("\n-- AVRO: things local parsers and SR may disagree on --")
tests = {
 "bad default (string field, int default)": {"type": "record", "name": "T1", "namespace": N, "fields": [{"name": "a", "type": "string", "default": 5}]},
 "top-level union": [ "null", {"type": "record", "name": "T2", "namespace": N, "fields": []}],
 "top-level enum": {"type": "enum", "name": "T3", "namespace": N, "symbols": ["A"]},
 "no namespace": {"type": "record", "name": "T4", "fields": []},
 "dotted name + conflicting namespace": {"type": "record", "name": f"{N}.real.T5", "namespace": "ignored.ns", "fields": []},
 "name with dash": {"type": "record", "name": "T-6", "namespace": N, "fields": []},
 "duplicate field": {"type": "record", "name": "T7", "namespace": N, "fields": [{"name": "a", "type": "int"}, {"name": "a", "type": "int"}]},
 "unknown logicalType": {"type": "record", "name": "T8", "namespace": N, "fields": [{"name": "a", "type": {"type": "string", "logicalType": "made-up"}}]},
 "empty namespace string": {"type": "record", "name": "T9", "namespace": "", "fields": []},
}
for label, s in tests.items():
    print(f"  {label:<40}", details(compat(f"{N}.T", s)))
