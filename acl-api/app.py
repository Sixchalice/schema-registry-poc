"""REST API in front of sr-acl-cli.

Every request runs the real CLI (`sr-acl-cli --config <file> --list|--add|--remove ...`) as a
subprocess, so validation and the add/remove merge rules are the CLI's own. Nothing here talks to
Kafka directly.

    GET    /health[?deep=true]          no auth; deep=true also runs `--list` (starts a JVM)
    GET    /operations                  operation names the CLI accepts
    GET    /acls[?principal=&subject=]  current ACLs (exact-match filters)
    POST   /acls                        --add    (body below)
    DELETE /acls                        --remove (body below)

    {"principals": ["c17.svc-a"], "subjects": ["orders-value"], "topics": ["orders"],
     "operations": ["SUBJECT_READ", "SUBJECT_WRITE"]}

POST/DELETE answer with the resulting operations of every (principal, subject) and global record
the command touched, read back with `--list` after the change.

Status codes: 400 = the CLI rejected the arguments (its own message is returned), 401 = bad
credentials, 502 = the CLI failed for another reason (e.g. Kafka), 504 = the CLI timed out.

Concurrency (run a single worker; each CLI run is a JVM costing ~1 s and ~2 CPU-seconds):
  * Reads are coalesced: at most one `--list` runs at a time. A request that arrives while one is
    running waits for the next run, which then answers every request that queued meanwhile. So a
    response never predates its request, and N concurrent readers cost one JVM, not N.
  * Writes are serialized (one `--add`/`--remove` JVM at a time). More than
    SR_ACL_MAX_PENDING_WRITES waiting writes get 503 + Retry-After instead of queueing.
  So at most two JVMs run at once, whatever the load.
Like the CLI itself, two writers (two instances, or this API and a CLI run elsewhere) changing the
same principal/subject at the same moment can lose an update: the CLI reads the ACL topic, merges,
and writes the result.

Environment:
    ACL_SERVICE_USER / ACL_SERVICE_PASSWORD  HTTP Basic credential (required)
    SR_ACL_CLI                 path to sr-acl-cli          (default /opt/sr-acl-cli/bin/sr-acl-cli)
    SR_ACL_CLI_CONFIG          SR properties for the CLI   (default /etc/acl-api/sr-acl-cli.properties)
    SR_ACL_CLI_TIMEOUT         seconds per CLI call        (default 60)
    SR_ACL_MAX_PENDING_WRITES  write queue limit           (default 20)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import sys
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger("acl-api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def _require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        sys.exit(f"{name} must be set")
    return value


CLI = os.environ.get("SR_ACL_CLI", "/opt/sr-acl-cli/bin/sr-acl-cli")
CONFIG = os.environ.get("SR_ACL_CLI_CONFIG", "/etc/acl-api/sr-acl-cli.properties")
TIMEOUT = float(os.environ.get("SR_ACL_CLI_TIMEOUT", "60"))
MAX_PENDING_WRITES = int(os.environ.get("SR_ACL_MAX_PENDING_WRITES", "20"))
API_USER = _require_env("ACL_SERVICE_USER").encode()
API_PASSWORD = _require_env("ACL_SERVICE_PASSWORD").encode()

# From `sr-acl-cli --help` (7.9.0). Only used for /operations and to shape POST/DELETE responses;
# the CLI does the actual validation.
SUBJECT_OPS = ["SUBJECT_READ", "SUBJECT_WRITE", "SUBJECT_DELETE",
               "SUBJECT_COMPATIBILITY_READ", "SUBJECT_COMPATIBILITY_WRITE"]
GLOBAL_OPS = ["GLOBAL_COMPATIBILITY_READ", "GLOBAL_COMPATIBILITY_WRITE", "GLOBAL_READ"]
OP_ALIASES = {"GLOBAL_SUBJECTS_READ": "GLOBAL_READ"}


# ---------------------------------------------------------------- request model

def _clean(value: str) -> str:
    # Control characters (newlines in particular) would break parsing of `--list` output.
    if not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("values must be non-blank and contain no control characters")
    return value


Value = Annotated[str, AfterValidator(_clean)]


class AclRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    principals: list[Value] = Field(min_length=1)
    subjects: list[Value] = []
    topics: list[Value] = []
    operations: list[Value] = Field(min_length=1)


# ---------------------------------------------------------------- running the CLI

class CliError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


async def run_cli(*args: str) -> str:
    """Runs sr-acl-cli and returns stdout; raises CliError on a non-zero exit or timeout."""
    # --opt=value keeps request values that start with '-' from being read as options.
    argv = [CLI, f"--config={CONFIG}", *args]
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()  # sr-acl-cli execs java, so this kills the JVM
        await proc.wait()
        log.error("sr-acl-cli timed out after %ss: %s", TIMEOUT, args)
        raise CliError(504, f"sr-acl-cli did not finish within {TIMEOUT:g}s")
    stdout, stderr = out.decode(errors="replace"), err.decode(errors="replace")
    if proc.returncode == 0:
        return stdout

    # Exit 1 means both "bad arguments" and "crashed". Bad arguments are reported as a message
    # followed by the usage table; anything else (a stack trace) is a server-side failure.
    lines = stderr.splitlines()
    usage_at = next((i for i, line in enumerate(lines)
                     if line.startswith(("Option ", "Usage:"))), None)
    if usage_at is not None:
        message = " ".join(line.strip() for line in lines[:usage_at] if line.strip())
        raise CliError(400, message or "sr-acl-cli rejected the arguments")
    log.error("sr-acl-cli exit %s for %s\n%s", proc.returncode, args, stderr)
    first = next((line.strip() for line in lines if line.strip() and not line.startswith("\t")),
                 f"exit {proc.returncode}")
    raise CliError(502, f"sr-acl-cli failed: {first}")


# ---------------------------------------------------------------- parsing `--list`

class _JavaMapParser:
    """Parses Java's Map.toString() as printed by `--list`, e.g.
    {p1={subj-a=[SUBJECT_READ], *=[SUBJECT_WRITE]}, p2={}}   or   {p1=[GLOBAL_READ]}.

    A key runs up to the next '={' or '=[', so keys may contain ', ' or '='; only a key that itself
    contains '={' or '=[' would be misread.
    """
    _KEY_END = re.compile(r"=(?=[{\[])")

    def __init__(self, text: str):
        self.s, self.i = text, 0

    def parse(self):
        value = self._value()
        if self.i != len(self.s):
            raise ValueError(f"trailing text at {self.i}")
        return value

    def _value(self):
        c = self.s[self.i:self.i + 1]
        if c == "{":
            return self._map()
        if c == "[":
            return self._list()
        raise ValueError(f"expected '{{' or '[' at {self.i}")

    def _map(self) -> dict:
        self.i += 1
        result: dict = {}
        if self.s.startswith("}", self.i):
            self.i += 1
            return result
        while True:
            m = self._KEY_END.search(self.s, self.i)
            if not m:
                raise ValueError(f"expected key at {self.i}")
            key = self.s[self.i:m.start()]
            self.i = m.end()
            result[key] = self._value()
            if self.s.startswith(", ", self.i):
                self.i += 2
            elif self.s.startswith("}", self.i):
                self.i += 1
                return result
            else:
                raise ValueError(f"expected ', ' or '}}' at {self.i}")

    def _list(self) -> list:
        end = self.s.index("]", self.i)
        body = self.s[self.i + 1:end]
        self.i = end + 1
        return [x for x in body.split(", ") if x]


def parse_list_output(stdout: str) -> tuple[dict, dict]:
    """-> ({principal: {subject: [ops]}}, {principal: [ops]})"""
    lines = [line.strip() for line in stdout.splitlines()]
    found: dict[str, dict] = {}
    for i, line in enumerate(lines):
        for kind in ("Subject", "Global"):
            if line.startswith(f"Current ACL's for {kind} Operations"):
                nxt = lines[i + 1] if i + 1 < len(lines) else ""
                found[kind] = _JavaMapParser(nxt).parse() if nxt.startswith("{") else {}
    if len(found) != 2:
        raise ValueError("headers not found")
    return found["Subject"], found["Global"]


async def _list_once() -> tuple[dict, dict]:
    stdout = await run_cli("--list")
    try:
        return parse_list_output(stdout)
    except (ValueError, IndexError) as e:
        log.error("unparseable sr-acl-cli --list output (%s):\n%s", e, stdout)
        raise CliError(502, f"could not parse sr-acl-cli --list output: {e}")


class ListBatcher:
    """At most one `--list` runs at a time, and every caller gets the result of a run that started
    after it called: if a run is in progress, callers queue for the next one, which serves them all."""

    def __init__(self):
        self._running: asyncio.Task | None = None
        self._next: asyncio.Future | None = None

    async def get(self) -> tuple[dict, dict]:
        if self._running is None:
            fut = self._launch()
        else:
            if self._next is None:
                self._next = asyncio.get_running_loop().create_future()
            fut = self._next
        # shield: a caller that goes away must not cancel the run the others are waiting for
        return await asyncio.shield(fut)

    def _launch(self) -> asyncio.Task:
        self._running = asyncio.create_task(_list_once())
        self._running.add_done_callback(self._finished)
        return self._running

    def _finished(self, task: asyncio.Task) -> None:
        task.exception()  # mark retrieved even if every caller has gone
        self._running = None
        if self._next is not None:
            queued, self._next = self._next, None
            self._launch().add_done_callback(
                lambda t: queued.set_exception(t.exception()) if t.exception() else queued.set_result(t.result()))


current_acls = ListBatcher().get


# ---------------------------------------------------------------- app

app = FastAPI(title="sr-acl-cli REST API", version="1.0")
basic = HTTPBasic(auto_error=False)
write_lock = asyncio.Lock()
pending_writes = 0


@app.exception_handler(CliError)
async def _cli_error(_: Request, e: CliError):
    return JSONResponse({"error": e.message}, status_code=e.status)


@app.exception_handler(HTTPException)
async def _http_error(_: Request, e: HTTPException):
    return JSONResponse({"error": e.detail}, status_code=e.status_code, headers=e.headers)


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, e: RequestValidationError):
    msgs = []
    for err in e.errors():
        where = "/".join(str(p) for p in err["loc"] if p != "body")
        msgs.append(f"{where}: {err['msg']}" if where else err["msg"])
    return JSONResponse({"error": "; ".join(msgs)}, status_code=400)


def authenticated(creds: HTTPBasicCredentials | None = Depends(basic)) -> str:
    ok = creds is not None and (
        secrets.compare_digest(creds.username.encode(), API_USER)
        & secrets.compare_digest(creds.password.encode(), API_PASSWORD))
    if not ok:
        raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": 'Basic realm="acl-api"'})
    return creds.username


@app.get("/health")
async def health(deep: bool = False):
    if not os.access(CLI, os.X_OK) or not os.path.isfile(CONFIG):
        return JSONResponse({"status": "DOWN", "error": "sr-acl-cli or its config is missing"}, 503)
    if deep:
        await current_acls()
    return {"status": "UP"}


@app.get("/operations", dependencies=[Depends(authenticated)])
async def operations():
    return {"subject": SUBJECT_OPS, "global": GLOBAL_OPS}


@app.get("/acls", dependencies=[Depends(authenticated)])
async def list_acls(principal: str | None = None, subject: str | None = None):
    by_subject, by_global = await current_acls()
    subject_acls = [
        {"principal": p, "subject": s, "operations": ops}
        for p, subjects in by_subject.items() if principal in (None, p)
        for s, ops in subjects.items() if subject in (None, s) and ops
    ]
    global_acls = [
        {"principal": p, "operations": ops}
        for p, ops in by_global.items() if principal in (None, p) and ops and subject is None
    ]
    subject_acls.sort(key=lambda a: (a["principal"], a["subject"]))
    global_acls.sort(key=lambda a: a["principal"])
    return {"subjectAcls": subject_acls, "globalAcls": global_acls}


async def acl_request(request: Request) -> AclRequest:
    """The JSON body, whatever the Content-Type (so plain `curl -d '{...}'` works too)."""
    try:
        return AclRequest.model_validate_json(await request.body() or b"null")
    except ValidationError as e:
        raise RequestValidationError(e.errors())


_body_doc = {"requestBody": {"required": True, "content": {
    "application/json": {"schema": AclRequest.model_json_schema()}}}}


@app.post("/acls", openapi_extra=_body_doc)
async def add_acls(req: AclRequest = Depends(acl_request), user: str = Depends(authenticated)):
    return await change("--add", req, user)


@app.delete("/acls", openapi_extra=_body_doc)
async def remove_acls(req: AclRequest = Depends(acl_request), user: str = Depends(authenticated)):
    return await change("--remove", req, user)


async def change(action: str, req: AclRequest, user: str):
    global pending_writes
    if pending_writes >= MAX_PENDING_WRITES:
        raise HTTPException(503, f"too many pending writes ({pending_writes}), retry later",
                            headers={"Retry-After": "5"})
    args = [action]
    args += [f"--principal={p}" for p in req.principals]
    args += [f"--subject={s}" for s in req.subjects]
    args += [f"--topic={t}" for t in req.topics]
    args += [f"--operation={o}" for o in req.operations]

    async def locked_write():
        global pending_writes
        try:
            async with write_lock:
                await run_cli(*args)
        finally:
            pending_writes -= 1
        log.info("user=%s %s", user, " ".join(args))

    pending_writes += 1
    # shield: if the client disconnects, the CLI still finishes before the lock is released
    await asyncio.shield(asyncio.create_task(locked_write()))
    by_subject, by_global = await current_acls()  # a run that starts after the write finished
    return touched(req, by_subject, by_global)


def touched(req: AclRequest, by_subject: dict, by_global: dict) -> dict:
    """The records the command wrote, with their resulting operations (empty after a full remove).
    Mirrors the CLI's expansion: '*' anywhere means all, topic T means subjects T-key and T-value."""
    def star(values: list[str]) -> list[str]:
        return ["*"] if "*" in values else list(dict.fromkeys(values))

    ops = {OP_ALIASES.get(o.upper(), o.upper()) for o in req.operations}
    if "*" in ops:
        ops = set(SUBJECT_OPS + GLOBAL_OPS)
    principals = star(req.principals)
    subjects = star(req.subjects + [s for t in req.topics
                                    for s in (["*"] if t == "*" else [f"{t}-key", f"{t}-value"])])
    subject_acls = [
        {"principal": p, "subject": s, "operations": by_subject.get(p, {}).get(s, [])}
        for p in principals for s in subjects
    ] if ops & set(SUBJECT_OPS) else []
    global_acls = [
        {"principal": p, "operations": by_global.get(p, [])} for p in principals
    ] if ops & set(GLOBAL_OPS) else []
    return {"subjectAcls": subject_acls, "globalAcls": global_acls}
