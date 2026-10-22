| # | Test | Expected | Actual | Result | Notes |
|---|------|----------|--------|--------|-------|
| a1 | GET /health without credentials | 200 | 200 | **PASS** |  |
| a2 | GET /acls without credentials | 401 | 401 | **PASS** |  |
| a3 | GET /acls with a wrong password | 401 | 401 | **PASS** |  |
| a4 | GET /acls as a Schema Registry user (not the service admin) | 401 | 401 | **PASS** |  |
| a5 | unknown route | 404 | 404 | **PASS** |  |
| a6 | unsupported method on /acls | 404 | 404 | **PASS** |  |
| a7 | GET /operations lists subject and global operations | SUBJECT_READ,SUBJECT_WRITE,SUBJECT_DELETE,SUBJECT_COMPATIBILITY_READ,SUBJECT_COMPATIBILITY_WRITE GLOBAL_COMPATIBILITY_READ,GLOBAL_COMPATIBILITY_WRITE,GLOBAL_READ | SUBJECT_READ,SUBJECT_WRITE,SUBJECT_DELETE,SUBJECT_COMPATIBILITY_READ,SUBJECT_COMPATIBILITY_WRITE GLOBAL_COMPATIBILITY_READ,GLOBAL_COMPATIBILITY_WRITE,GLOBAL_READ | **PASS** |  |
| a8 | service and CLI list the same ACLs | c17.svc-a\|orders-key\|SUBJECT_READ c17.svc-a\|orders-value\|SUBJECT_READ platform-admin\|*\|SUBJECT_COMPATIBILITY_READ,SUBJECT_COMPATIBILITY_WRITE,SUBJECT_DELETE,SUBJECT_READ,SUBJECT_WRITE platform-admin\|GLOBAL\|GLOBAL_COMPATIBILITY_READ,GLOBAL_COMPATIBILITY_WRITE,GLOBAL_READ | c17.svc-a\|orders-key\|SUBJECT_READ c17.svc-a\|orders-value\|SUBJECT_READ platform-admin\|*\|SUBJECT_COMPATIBILITY_READ,SUBJECT_COMPATIBILITY_WRITE,SUBJECT_DELETE,SUBJECT_READ,SUBJECT_WRITE platform-admin\|GLOBAL\|GLOBAL_COMPATIBILITY_READ,GLOBAL_COMPATIBILITY_WRITE,GLOBAL_READ | **PASS** |  |
| a9 | add SUBJECT_READ on topic acltest1792419557 | 200 | 200 | **PASS** | {  "subjectAcls" : [ {    "principal" : "acl-test-1792419557",    "subject" : "acltest1792419557-key",    "operations" : [ "SUBJECT_READ" ]  }, {    " |
| a10 | topic acltest1792419557 means subjects acltest1792419557-key and acltest1792419557-value | SUBJECT_READ SUBJECT_READ | SUBJECT_READ SUBJECT_READ | **PASS** |  |
| a11 | CLI sees the service's ACLs | acl-test-1792419557\|acltest1792419557-key\|SUBJECT_READ acl-test-1792419557\|acltest1792419557-value\|SUBJECT_READ c17.svc-a\|orders-key\|SUBJECT_READ c17.svc-a\|orders-value\|SUBJECT_READ platform-admin\|*\|SUBJECT_COMPATIBILITY_READ,SUBJECT_COMPATIBILITY_WRITE,SUBJECT_DELETE,SUBJECT_READ,SUBJECT_WRITE platform-admin\|GLOBAL\|GLOBAL_COMPATIBILITY_READ,GLOBAL_COMPATIBILITY_WRITE,GLOBAL_READ | acl-test-1792419557\|acltest1792419557-key\|SUBJECT_READ acl-test-1792419557\|acltest1792419557-value\|SUBJECT_READ c17.svc-a\|orders-key\|SUBJECT_READ c17.svc-a\|orders-value\|SUBJECT_READ platform-admin\|*\|SUBJECT_COMPATIBILITY_READ,SUBJECT_COMPATIBILITY_WRITE,SUBJECT_DELETE,SUBJECT_READ,SUBJECT_WRITE platform-admin\|GLOBAL\|GLOBAL_COMPATIBILITY_READ,GLOBAL_COMPATIBILITY_WRITE,GLOBAL_READ | **PASS** |  |
| a12 | add merges into the existing set | 200 SUBJECT_READ,SUBJECT_WRITE | 200 SUBJECT_READ,SUBJECT_WRITE | **PASS** |  |
| a13 | add is idempotent | 200 SUBJECT_READ,SUBJECT_WRITE | 200 SUBJECT_READ,SUBJECT_WRITE | **PASS** |  |
| a14 | remove takes out only the given operation | 200 SUBJECT_READ | 200 SUBJECT_READ | **PASS** |  |
| a15 | operation names are case-insensitive | 200 SUBJECT_READ,SUBJECT_DELETE | 200 SUBJECT_READ,SUBJECT_DELETE | **PASS** |  |
| a16 | global operation, alias GLOBAL_SUBJECTS_READ -> GLOBAL_READ | 200 GLOBAL_READ | 200 GLOBAL_READ | **PASS** |  |
| a17 | '*' operations on a subject grant every subject and global operation | 200 SUBJECT_READ,SUBJECT_WRITE,SUBJECT_DELETE,SUBJECT_COMPATIBILITY_READ,SUBJECT_COMPATIBILITY_WRITE GLOBAL_COMPATIBILITY_READ,GLOBAL_COMPATIBILITY_WRITE,GLOBAL_READ | 200 SUBJECT_READ,SUBJECT_WRITE,SUBJECT_DELETE,SUBJECT_COMPATIBILITY_READ,SUBJECT_COMPATIBILITY_WRITE GLOBAL_COMPATIBILITY_READ,GLOBAL_COMPATIBILITY_WRITE,GLOBAL_READ | **PASS** |  |
| a18 | filter by principal and subject (no global rows) | 1 0 | 1 0 | **PASS** |  |
| a19 | removing every operation hides the ACL from the list | 200 - | 200 - | **PASS** |  |
| a20 | remove on a principal without ACLs is a no-op | 200 - | 200 - | **PASS** |  |
| a21 | no principals | 400 | 400 | **PASS** | {  "error" : "principals is required"} |
| a22 | no operations | 400 | 400 | **PASS** | {  "error" : "operations is required"} |
| a23 | subject operation without subject or topic | 400 | 400 | **PASS** | {  "error" : "subjects or topics are required for subject operations"} |
| a24 | global-only operation with a subject | 400 | 400 | **PASS** | {  "error" : "subjects/topics can't be given for global operations only"} |
| a25 | unknown operation | 400 | 400 | **PASS** | {  "error" : "invalid operation: NOPE"} |
| a26 | operation the CLI can't set (EXPORTER_ACCESS) | 400 | 400 | **PASS** | {  "error" : "invalid operation: EXPORTER_ACCESS"} |
| a27 | blank principal | 400 | 400 | **PASS** | {  "error" : "principals must not contain blank values"} |
| a28 | unknown JSON field | 400 | 400 | **PASS** | {  "error" : "invalid JSON body: Unrecognized field \"subject\" (class poc.acl.AclService$AclRequest), not marked as ignorable"} |
| a29 | malformed JSON | 400 | 400 | **PASS** | {  "error" : "invalid JSON body: Unrecognized token 'not': was expecting (JSON String, Number, Array, Object or token 'null', 'true' or 'false')"} |
| a30 | rejected requests wrote nothing | - | - | **PASS** |  |
| a31 | precondition: c17.svc-b can't read orders-value | 403 | 403 | **PASS** | {"error_code":40301,"message":"User is denied operation Read on Subject: orders-value"} |
| a32 | grant c17.svc-b SUBJECT_READ on topic orders | 200 | 200 | **PASS** | {  "subjectAcls" : [ {    "principal" : "c17.svc-b",    "subject" : "orders-key",    "operations" : [ "SUBJECT_READ" ]  }, {    "principal" : "c17.svc |
| a33 | c17.svc-b can read orders-value | 200 within 10000 ms | 22 ms | **PASS** |  |
| a34 | revoke it | 200 | 200 | **PASS** | {  "subjectAcls" : [ {    "principal" : "c17.svc-b",    "subject" : "orders-key",    "operations" : [ ]  }, {    "principal" : "c17.svc-b",    "subjec |
| a35 | c17.svc-b is denied again | 403 within 10000 ms | 21 ms | **PASS** |  |
| a36 | CLI --add visible in GET /acls | <= 10000 ms | 46 ms | **PASS** |  |
| a37 | service add merges with the CLI's grant | 200 GLOBAL_COMPATIBILITY_READ,GLOBAL_READ | 200 GLOBAL_COMPATIBILITY_READ,GLOBAL_READ | **PASS** |  |
| a38 | CLI --remove visible in GET /acls | <= 10000 ms | 49 ms | **PASS** |  |
| a39 | 5 parallel single-operation adds -> all 5 operations | SUBJECT_READ,SUBJECT_WRITE,SUBJECT_DELETE,SUBJECT_COMPATIBILITY_READ,SUBJECT_COMPATIBILITY_WRITE | SUBJECT_READ,SUBJECT_WRITE,SUBJECT_DELETE,SUBJECT_COMPATIBILITY_READ,SUBJECT_COMPATIBILITY_WRITE | **PASS** |  |
| a40 | service healthy again after restart | <= 60000 ms | 2077 ms | **PASS** |  |
| a41 | same ACLs after restart | acl-test-1792419557\|acltest-1792419557-race\|SUBJECT_COMPATIBILITY_READ,SUBJECT_COMPATIBILITY_WRITE,SUBJECT_DELETE,SUBJECT_READ,SUBJECT_WRITE acl-test-1792419557\|acltest1792419557-key\|SUBJECT_READ acl-test-1792419557\|acltest1792419557-value\|SUBJECT_READ c17.svc-a\|orders-key\|SUBJECT_READ c17.svc-a\|orders-value\|SUBJECT_READ platform-admin\|*\|SUBJECT_COMPATIBILITY_READ,SUBJECT_COMPATIBILITY_WRITE,SUBJECT_DELETE,SUBJECT_READ,SUBJECT_WRITE platform-admin\|GLOBAL\|GLOBAL_COMPATIBILITY_READ,GLOBAL_COMPATIBILITY_WRITE,GLOBAL_READ | acl-test-1792419557\|acltest-1792419557-race\|SUBJECT_COMPATIBILITY_READ,SUBJECT_COMPATIBILITY_WRITE,SUBJECT_DELETE,SUBJECT_READ,SUBJECT_WRITE acl-test-1792419557\|acltest1792419557-key\|SUBJECT_READ acl-test-1792419557\|acltest1792419557-value\|SUBJECT_READ c17.svc-a\|orders-key\|SUBJECT_READ c17.svc-a\|orders-value\|SUBJECT_READ platform-admin\|*\|SUBJECT_COMPATIBILITY_READ,SUBJECT_COMPATIBILITY_WRITE,SUBJECT_DELETE,SUBJECT_READ,SUBJECT_WRITE platform-admin\|GLOBAL\|GLOBAL_COMPATIBILITY_READ,GLOBAL_COMPATIBILITY_WRITE,GLOBAL_READ | **PASS** |  |
