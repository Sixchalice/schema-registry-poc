| # | Test | Expected | Actual | Result | Notes |
|---|------|----------|--------|--------|-------|
| d1 | platform-admin sets DR to IMPORT | 200 | 200 | **PASS** | {"mode":"IMPORT"} |
| d2 | platform-admin creates exporter 'dr' on primary | 200 | 200 | **PASS** | {"name":"dr"} |
| d3 | all primary schemas on DR with the same subject/version/ID | <= 90000 ms | 716 ms | **PASS** | 2 schemas; exporter status {"name":"dr","state":"RUNNING","offset":24,"ts":1791988442116,"deksOffset":-1,"deksTs":0} |
| d4 | c17.svc-a GET orders-value on DR | 200 | 200 | **PASS** |  |
| d5 | c17.svc-a GET /schemas/ids/1 on DR | 200 | 200 | **PASS** |  |
| d6 | c17.svc-b GET orders-value on DR | 403 | 403 | **PASS** |  |
| d7 | c17.svc-a registers on DR | 403 | 403 | **PASS** |  |
| d8 | platform-admin registers on DR while IMPORT | 422 | 422 | **PASS** | {"error_code":42205,"message":"Subject dr-test-1791988571-import-value is not in read-write mode"} |
| d9 | platform-admin registers dr-test-1791988571-value on primary | 200 | 200 | **PASS** |  |
| d10 | new schema reaches DR with the same ID | <= 60000 ms | 50 ms | **PASS** | id 5 |
| d11 | per-subject compatibility NONE reaches DR | exported? | 578 ms | **INFO** | DR GET /config/dr-test-1791988571-value: 200 {"compatibilityLevel":"NONE"} |
| d12 | ACL added on primary: c17.svc-b reads dr-test-1791988571-value there | 200 | 200 | **PASS** |  |
| d13 | same ACL on DR: c17.svc-b reads dr-test-1791988571-value there | 403 | 403 | **PASS** | ACLs are not exported |
| d14 | pause exporter 'dr' | 200 | 200 | **PASS** | {"name":"dr"} |
| d15 | DR to READWRITE | 200 | 200 | **PASS** | {"mode":"READWRITE"} |
| d16 | platform-admin registers dr-test-1791988571-failover-value on DR | 200 | 200 | **PASS** | {"id":6} |
| d17 | the new DR ID is unused on the primary | 404 | 404 | **PASS** | DR assigned 6; primary max ID 5 |
| d18 | primary to IMPORT (force) | 200 | 200 | **PASS** | {"mode":"IMPORT"} |
| d19 | platform-admin creates exporter 'dr-back' on DR | 200 | 200 | **PASS** | {"name":"dr-back"} |
| d20 | schema registered during the outage reaches the primary, same ID | <= 60000 ms | 616 ms | **PASS** | id 6 |
| d21 | swap back: pause+delete dr-back, primary READWRITE, DR IMPORT, resume dr | 200 204 200 200 200 | 200 204 200 200 200 | **PASS** | {"name":"dr"} |
| d22 | platform-admin registers on primary after failback | 200 | 200 | **PASS** | {"id":7} |
| d23 | primary and DR hold identical schemas again | <= 60000 ms | 86 ms | **PASS** |  |
