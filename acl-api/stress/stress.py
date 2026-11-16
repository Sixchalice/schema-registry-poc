#!/usr/bin/env python3
"""Stress test for acl-api. Standard library only.

    python3 stress.py read  --levels 1,5,10,25,50,100 --duration 20
    python3 stress.py write --levels 1,5,20,40 --duration 30
    python3 stress.py mixed --levels 12,60 --duration 30     # 1 writer per 6 clients
    python3 stress.py burst --levels 20,100                  # N principals -> same subject at once

At each concurrency level, N client threads send requests back to back for --duration seconds.
Writers each POST then DELETE an ACL on a subject of their own (principal stress-<run>):
  * a POST answered 200 whose read-back doesn't show the added operation counts as "lost";
  * at the end, GET /acls must show no ACL left for stress-<run>.
Prints one row per level and request kind: requests, ok/s, latency percentiles, status counts.
"""
import argparse
import base64
import http.client
import json
import threading
import time
import urllib.parse
from collections import Counter, defaultdict


def client(base, auth, timeout):
    u = urllib.parse.urlparse(base)
    hdr = {"Authorization": "Basic " + base64.b64encode(auth.encode()).decode(),
           "Content-Type": "application/json"}
    conn = [http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)]

    def call(method, path, body=None):
        t0 = time.perf_counter()
        try:
            conn[0].request(method, path, body=json.dumps(body) if body is not None else None, headers=hdr)
            r = conn[0].getresponse()
            return r.status, time.perf_counter() - t0, r.read()
        except Exception as e:  # timeouts, resets: report the exception name as the status
            conn[0].close()
            conn[0] = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
            return type(e).__name__, time.perf_counter() - t0, b""
    return call


def pct(vals, p):
    return vals[min(len(vals) - 1, int(p / 100 * len(vals)))] if vals else float("nan")


def run_level(args, n, mode, run_id):
    stop = time.monotonic() + args.duration
    lat, codes, lock = defaultdict(list), defaultdict(Counter), threading.Lock()

    def worker(i):
        call = client(args.url, args.auth, args.timeout)
        writer = mode == "write" or (mode == "mixed" and i % 6 == 0)
        k = 0
        while time.monotonic() < stop:
            got = []
            if writer:
                body = {"principals": [f"stress-{run_id}"], "subjects": [f"stress-{run_id}-{n}-{i}-{k}"],
                        "operations": ["SUBJECT_READ"]}
                st, dt, data = call("POST", "/acls", body)
                if st == 200 and "SUBJECT_READ" not in json.loads(data)["subjectAcls"][0]["operations"]:
                    st = "lost"
                got.append(("POST", st, dt))
                if st in (200, "lost"):  # clean up: retry the remove until the queue has room
                    while (r := call("DELETE", "/acls", body))[0] == 503:
                        got.append(("DELETE", *r[:2]))
                        time.sleep(1)
                    got.append(("DELETE", *r[:2]))
                k += 1
            else:
                st, dt, _ = call("GET", "/acls")
                got.append(("GET", st, dt))
            with lock:
                for kind, st, dt in got:
                    codes[kind][st] += 1
                    lat[kind].append(dt)

    t0 = time.monotonic()
    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.monotonic() - t0
    for kind in sorted(lat):
        v = sorted(lat[kind])
        print(f"{mode:5} c={n:<4} {kind:6} reqs={len(v):<5} ok/s={codes[kind][200] / wall:6.2f}  "
              f"p50={pct(v, 50):6.2f}s p95={pct(v, 95):6.2f}s max={v[-1]:6.2f}s  codes={dict(codes[kind])}",
              flush=True)


def run_burst(args, n, run_id):
    """N different principals ask for SUBJECT_READ on the same subject at the same instant, then all
    remove it again at the same instant. Reports the time until the whole burst was answered."""
    subject = f"burst-{run_id}-{n}"
    for method in ("POST", "DELETE"):
        barrier = threading.Barrier(n)
        results = [None] * n

        def worker(i):
            call = client(args.url, args.auth, args.timeout)
            body = {"principals": [f"burst-{run_id}-{n}-{i}"], "subjects": [subject], "operations": ["SUBJECT_READ"]}
            barrier.wait()
            while True:  # a 503 is retried after its Retry-After, like a well-behaved client
                status, dt, data = call(method, "/acls", body)
                if status != 503:
                    break
                time.sleep(1)
            if status == 200 and method == "POST" and "SUBJECT_READ" not in json.loads(data)["subjectAcls"][0]["operations"]:
                status = "lost"
            results[i] = (status, time.perf_counter())

        start = time.perf_counter()
        threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        finish = sorted(done - start for _, done in results)
        codes = Counter(status for status, _ in results)
        print(f"burst n={n:<4} {method:6} all answered in {finish[-1]:6.2f}s  "
              f"(p50 {pct(finish, 50):5.2f}s)  codes={dict(codes)}", flush=True)

    status, _, data = client(args.url, args.auth, args.timeout)("GET", f"/acls?subject={subject}")
    left = len(json.loads(data)["subjectAcls"]) if status == 200 else f"? (status {status})"
    print(f"burst n={n:<4} leftover ACLs on {subject}: {left}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["read", "write", "mixed", "burst"])
    ap.add_argument("--url", default="http://localhost:8087")
    ap.add_argument("--auth", default="acl-admin:acl-admin-secret")
    ap.add_argument("--levels", default="1,5,10,25,50")
    ap.add_argument("--duration", type=float, default=20)
    ap.add_argument("--timeout", type=float, default=120, help="client-side request timeout (s)")
    args = ap.parse_args()
    run_id = str(int(time.time()))

    for n in [int(x) for x in args.levels.split(",")]:
        if args.mode == "burst":
            run_burst(args, n, run_id)
        else:
            run_level(args, n, args.mode, run_id)

    if args.mode in ("write", "mixed"):
        status, _, data = client(args.url, args.auth, args.timeout)("GET", f"/acls?principal=stress-{run_id}")
        left = len(json.loads(data)["subjectAcls"]) if status == 200 else f"? (status {status})"
        print(f"leftover ACLs for stress-{run_id}: {left}")


if __name__ == "__main__":
    main()
