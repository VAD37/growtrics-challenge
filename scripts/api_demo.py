#!/usr/bin/env python3
"""The whole user-facing API as one narrated transcript, request by request.

What it proves
    One learner sends one instruction over HTTP and gets back video bytes that are, byte for
    byte, a lesson committed to this repository. Every call in between is printed: the method
    and path, the headers that carry meaning, the body sent, the status, the milliseconds, the
    body returned. Nothing is claimed that the run did not watch happen.

What it needs running first
    `make up`, finished: db, storage, api and worker all answering. The API is on
    http://localhost:8000 by default. Expect roughly a minute of wall clock -- the mock
    generator pretends to render for 10 to 60 seconds (D107) so a job can be caught mid-flight.

How to run
    python scripts/api_demo.py
    python scripts/api_demo.py --base-url http://some-host:8000 --out-dir ./demo-out
    python scripts/api_demo.py --instruction "what is a mole" --no-color
    python scripts/api_demo.py --full

Steps 0 to 8 are the default walk and every one of them is on the path a learner takes. `--full`
adds steps 9 and 10, which are not: a `FAIL_ME` job polled to `FAILED`, and four submits under a
caller minted for the step to provoke `429 TOO_MANY_ACTIVE_JOBS`. Those are four more renders on
a worker that runs one job at a time, and step 10 waits for the three it started rather than
leaving them in front of whatever runs next, so the flag roughly triples the wall clock (D114).

Standard library only, Python 3.9 or newer. No `uv sync`, no `requests`, no `jq`, and nothing
imported from `src/app`: a client assembled out of the server's own code proves less than one
that never sees it. Exit status is 0 only when the job actually succeeded and the primary
artifact actually downloaded.

`scripts/demo.sh` and `scripts/demo.ps1` are the short walk and stay the reference for which
endpoints exist. This is the long one: it downloads every artifact rather than only the video,
checks each against its own `ETag`, and shows the 404 next to the happy path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

BODY_PREVIEW_CHARS = 2400
"""How much of a pretty-printed response to show before saying how much was left."""

HEX_PREVIEW_BYTES = 24
TERMINAL_STATUSES = ("SUCCEEDED", "FAILED", "CANCELLED")
RULE = "=" * 78

# Crockford base32 body, 26 characters, so this is a well-formed id that no ULID will ever be.
ABSENT_JOB_ID = "job_" + "0" * 26

FIXTURE_LESSONS = ("covalent_bonds.mp4", "ionic_vs_covalent.mp4", "ph_scale.mp4")
FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src",
    "app",
    "generation",
    "backends",
    "fixtures",
)

EXTENSIONS = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "text/plain": ".txt",
    "text/vtt": ".vtt",
    "application/json": ".json",
}

TEXTUAL_PREFIXES = ("text/", "application/json", "application/problem+json")


class DemoFailure(Exception):
    """The run cannot continue. The message is one line and is the whole explanation."""


class Ink:
    """ANSI colour, or plain ASCII when the output is not going to a terminal.

    Redirected output is the common case for a transcript somebody pastes into a report, and
    escape codes in a paste are worse than no colour at all.
    """

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)


@dataclass(frozen=True)
class Reply:
    """One HTTP answer, kept whole so the printer and the caller read the same thing.

    `headers` is keyed lowercase. Header names are case-insensitive on the wire and uvicorn
    sends them lowercase, so a client that looks up `ETag` verbatim finds nothing and then
    reports, cheerfully and wrongly, that the server sent no tag.
    """

    status: int
    headers: dict
    body: bytes
    elapsed_ms: float

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")

    @property
    def content_type(self) -> str:
        return self.header("Content-Type")

    @property
    def is_textual(self) -> bool:
        return any(self.content_type.startswith(prefix) for prefix in TEXTUAL_PREFIXES)

    def json(self) -> Any:
        """The parsed body, or `None` when it was not JSON. Decoding never raises: bad bytes
        become replacement characters, so only the parse can fail and one `except` covers it."""
        try:
            return json.loads(self.body.decode("utf-8", errors="replace"))
        except ValueError:
            return None


@dataclass(frozen=True)
class Download:
    """An artifact that reached the disk, and what it turned out to be."""

    role: str
    media_type: str
    artifact_id: str
    path: str
    size_bytes: int
    sha256: str
    etag: str
    verified: bool


def truncate(text: str, limit: int) -> str:
    """Cut long output, and say in bytes exactly how much was cut.

    A body that stops mid-sentence with no marker reads like the server sent a short answer.
    """
    if len(text) <= limit:
        return text
    dropped = len(text[limit:].encode("utf-8"))
    return f"{text[:limit]}\n... ({dropped} more bytes)"


def lowercase_headers(items) -> dict:
    """Header names are case-insensitive on the wire, so they are one case in here."""
    return {str(name).lower(): value for name, value in items}


def indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def hex_preview(payload: bytes, count: int = HEX_PREVIEW_BYTES) -> str:
    head = payload[:count]
    rendered = " ".join(f"{byte:02x}" for byte in head)
    if len(payload) > count:
        rendered += f" ... ({len(payload) - count} more bytes)"
    return rendered


def pretty_json(payload: bytes) -> str:
    """Indented JSON, or the text as it arrived when it was not JSON at all."""
    text = payload.decode("utf-8", errors="replace")
    try:
        return json.dumps(json.loads(text), indent=2, sort_keys=False)
    except ValueError:
        return text


class Api:
    """The client, and the only thing in this file that prints an HTTP call.

    Every call goes through `send`, so the transcript cannot drift into two shapes depending on
    which step made the request.
    """

    def __init__(self, base_url: str, user_id: str, ink: Ink) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id
        self.ink = ink

    def send(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        announce: bool = True,
    ) -> Reply:
        url = self.base_url + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        # Identity rides at the credential position and never in the body. There is no
        # authentication in this build; the header is a claim the server takes at face value.
        request.add_header("X-User-Id", self.user_id)
        if data is not None:
            request.add_header("Content-Type", "application/json")

        if announce:
            self._print_request(method, path, data)

        started = time.monotonic()
        try:
            with urllib.request.urlopen(request) as response:
                reply = Reply(
                    status=response.status,
                    headers=lowercase_headers(response.headers.items()),
                    body=response.read(),
                    elapsed_ms=(time.monotonic() - started) * 1000.0,
                )
        except urllib.error.HTTPError as error:
            # A 4xx is data here, not an accident: step 7 goes looking for one.
            reply = Reply(
                status=error.code,
                headers=lowercase_headers(error.headers.items()),
                body=error.read(),
                elapsed_ms=(time.monotonic() - started) * 1000.0,
            )
        except urllib.error.URLError as error:
            raise DemoFailure(
                f"cannot reach {self.base_url} ({error.reason}) -- `make up` may not have "
                "finished, or the API is not listening on that address"
            ) from None

        if announce:
            self._print_reply(reply)
        return reply

    def _print_request(self, method: str, path: str, data: bytes | None) -> None:
        ink = self.ink
        print(ink.bold(f"--> {method} {self.base_url}{path}"))
        print(ink.dim(f"    X-User-Id: {self.user_id}"))
        if data is not None:
            print(ink.dim("    Content-Type: application/json"))
            print(indent(truncate(pretty_json(data), BODY_PREVIEW_CHARS)))

    def _print_reply(self, reply: Reply) -> None:
        ink = self.ink
        paint = ink.green if reply.status < 400 else ink.red
        status = paint(f"<-- {reply.status}")
        timing = ink.dim(f"{reply.elapsed_ms:.1f} ms, {len(reply.body)} bytes")
        print(f"{status}  {timing}")
        # The headers that carry meaning here: where the new resource lives, what the server
        # says the bytes hash to, and what a browser would call the file.
        for header in ("Content-Type", "Location", "ETag", "Content-Disposition"):
            value = reply.header(header)
            if value:
                print(ink.dim(f"    {header}: {value}"))

        if not reply.body:
            print(ink.dim("    (no body)"))
        elif reply.is_textual:
            print(indent(truncate(pretty_json(reply.body), BODY_PREVIEW_CHARS)))
        else:
            # Never the raw bytes. A video written to a terminal is a wrecked terminal.
            kind = reply.content_type or "unknown type"
            print(ink.dim(f"    binary: {kind}, {len(reply.body)} bytes"))
            print(ink.dim(f"    first bytes: {hex_preview(reply.body)}"))
        print()


def heading(ink: Ink, number: str, title: str) -> None:
    print()
    print(ink.cyan(RULE))
    print(ink.cyan(f"STEP {number}  {title}"))
    print(ink.cyan(RULE))


def note(ink: Ink, text: str) -> None:
    print(ink.yellow("    " + text))


def sha256_of_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def etag_digest(etag: str) -> str | None:
    """The hex out of `"sha256:..."`, or `None` when the tag is not a content hash."""
    cleaned = etag.strip().removeprefix("W/").strip('"')
    if cleaned.startswith("sha256:"):
        return cleaned[len("sha256:") :]
    return None


def filename_for(role: str, media_type: str, taken: set) -> str:
    """A name a reader can match to a row in the artifact table without being told."""
    extension = EXTENSIONS.get(media_type.split(";")[0].strip(), ".bin")
    stem = role.lower() or "artifact"
    candidate = stem + extension
    counter = 2
    while candidate in taken:
        candidate = f"{stem}-{counter}{extension}"
        counter += 1
    taken.add(candidate)
    return candidate


# --------------------------------------------------------------------------------- the steps


def step_health(api: Api) -> None:
    heading(api.ink, "0", "GET /health -- is anything answering")
    reply = api.send("GET", "/health")
    if reply.status != 200:
        raise DemoFailure(f"the service is not healthy (HTTP {reply.status})")


def step_submit(api: Api, instruction: str) -> str:
    heading(api.ink, "1", "POST /v1/jobs -- the submit")
    print("    In plain words, a grade 9 learner is asking for a short chemistry video about:")
    print(api.ink.bold(f"        {instruction}"))
    print("    That is the whole request. The client sends no prompt, no file paths and no")
    print("    output contract -- it names a profile and the server resolves what that means.")
    print()

    body = {
        "instruction": instruction,
        "context": [{"kind": "LEVEL", "text": "grade 9"}],
    }
    reply = api.send("POST", "/v1/jobs", body)

    if reply.status == 429:
        raise DemoFailure(
            "the server refused this job: three jobs from this caller are already QUEUED or "
            "RUNNING, which is the admission cap (D090). Let one finish, or pass a different "
            "--user-id"
        )
    if reply.status not in (200, 201, 202):
        raise DemoFailure(f"submit failed with HTTP {reply.status}")

    document = reply.json() or {}
    job_id = document.get("job_id")
    if not job_id:
        raise DemoFailure(f"the submit answered {reply.status} with no job_id")

    print(api.ink.bold(f"    job id: {job_id}"))
    print("    Accepted, not done. Nothing rendered inside that request; a worker will pick")
    print("    it up off the queue. The job id is the whole receipt.")
    return job_id


def poll_until_terminal(api: Api, job_id: str, poll_seconds: float, poll_timeout: float) -> dict:
    """Poll one job to any terminal status and return its last document, `_elapsed_s` stamped.

    Which terminal status is acceptable is the caller's business, not this loop's: step 2 wants
    `SUCCEEDED` and step 8 wants `FAILED`, and both want the same transcript on the way there.
    """
    ink = api.ink
    started = time.monotonic()
    previous = None
    document: dict = {}
    first = True

    while True:
        elapsed = time.monotonic() - started
        reply = api.send("GET", f"/v1/jobs/{job_id}", announce=first)
        if reply.status != 200:
            raise DemoFailure(f"reading the job failed with HTTP {reply.status}")

        document = reply.json() or {}
        status = str(document.get("status", "?"))
        stage = str(document.get("stage", "?"))
        percent = (document.get("progress") or {}).get("percent", 0)
        current = (status, stage, percent)

        if previous is None:
            mark = "  * first"
        elif current != previous:
            mark = "  * changed"
        else:
            mark = ""
        call = f"GET /v1/jobs/{job_id} -> {reply.status}"
        state = f"{status:<10} {stage:<12} {percent:>3}%"
        line = f"    t+{elapsed:6.1f}s  {call}   {state}{mark}"
        print(ink.bold(line) if mark else line)
        first = False
        previous = current

        if status in TERMINAL_STATUSES:
            break
        if elapsed > poll_timeout:
            raise DemoFailure(
                f"the job was still {status} after {poll_timeout:.0f}s (--poll-timeout)"
            )
        time.sleep(poll_seconds)

    total = time.monotonic() - started
    print()
    print(ink.bold("    final job document"))
    print(indent(truncate(json.dumps(document, indent=2), BODY_PREVIEW_CHARS)))
    print()
    print(
        ink.bold(
            f"    total wall clock from submit to terminal: {total:.1f}s "
            "(the mock generator sleeps 10 to 60s on purpose)"
        )
    )
    document["_elapsed_s"] = total
    return document


def step_poll(api: Api, job_id: str, poll_seconds: float, poll_timeout: float) -> dict:
    heading(api.ink, "2", f"GET /v1/jobs/{job_id} -- poll until it stops moving")
    print("    One line per poll. A line is marked when the status, stage or percent moved,")
    print("    so the state machine is visible rather than described. The first and the last")
    print("    poll print the whole job document; the ones in between would just repeat it.")
    print()

    document = poll_until_terminal(api, job_id, poll_seconds, poll_timeout)
    status = str(document.get("status"))
    if status != "SUCCEEDED":
        failure = document.get("failure") or {}
        code = failure.get("code", "no code")
        message = failure.get("message", "no message")
        raise DemoFailure(f"the job ended {status}: {code} -- {message}")
    return document


def step_list_jobs(api: Api) -> None:
    heading(api.ink, "3", "GET /v1/jobs -- every job in the system, newest first")
    print("    Not scoped to the X-User-Id above, despite the header being sent: the listing")
    print("    applies no ownership predicate, so it returns everybody's jobs. That is a known")
    print("    hole and it is marked @audit at orchestration/service.py::ListJobs. D111 decided")
    print("    the single-job read and deliberately left this one open.")
    print("    Listed jobs carry artifact: null even when they have one; the detail read in")
    print("    step 2 is what fills that in.")
    print()
    reply = api.send("GET", "/v1/jobs")
    if reply.status != 200:
        raise DemoFailure(f"listing jobs failed with HTTP {reply.status}")
    page = reply.json() or {}
    print(f"    {len(page.get('items') or [])} job(s) in this page")


def step_list_artifacts(api: Api, job_id: str) -> list:
    ink = api.ink
    heading(ink, "4", f"GET /v1/artifacts?job_id={job_id} -- what the job produced")
    reply = api.send("GET", f"/v1/artifacts?job_id={job_id}")
    if reply.status != 200:
        raise DemoFailure(f"listing artifacts failed with HTTP {reply.status}")

    items = (reply.json() or {}).get("items") or []
    if not items:
        raise DemoFailure("the job succeeded and published nothing")

    print(ink.bold(f"    {'ROLE':<12}{'MEDIA TYPE':<14}{'BYTES':>12}  ARTIFACT ID"))
    for item in items:
        role = item.get("role", "?")
        media_type = item.get("media_type", "?")
        size = item.get("size_bytes", 0)
        print(f"    {role:<12}{media_type:<14}{size:>12}  {item.get('artifact_id', '?')}")
    print()

    primary = [item for item in items if item.get("role") == "PRIMARY"]
    if not primary:
        raise DemoFailure("the job succeeded but published no PRIMARY artifact")
    note(ink, f"The video is the row whose role is PRIMARY: {primary[0]['artifact_id']}")
    note(
        ink,
        "The operator log custody also wrote is not in this listing. The listing is "
        "learner-facing,",
    )
    note(ink, "and that exclusion lives in a partial index rather than in a filter (D073, A6).")

    print()
    print(ink.bold("    the same endpoint with the filter taken off: GET /v1/artifacts"))
    print("    Not this caller's artifacts, despite X-User-Id being sent. ListArtifacts applies")
    print("    no ownership predicate, so this is every learner-facing artifact in the database,")
    print("    whoever made it -- the same hole the job listing has in step 3, marked @audit at")
    print("    orchestration/service.py::ListArtifacts. What it does exclude is quarantined and")
    print("    operator rows, which is the partial index and not an ownership check.")
    print()
    everything = api.send("GET", "/v1/artifacts")
    if everything.status != 200:
        raise DemoFailure(f"the unfiltered artifact listing failed with HTTP {everything.status}")
    page = everything.json() or {}
    listed = len(page.get("items") or [])
    tail = page.get("next_cursor")
    print(f"    {listed} artifact(s) in this page (default limit 20)")
    print(f"    next_cursor {tail or '(none -- this page is all of them)'}")
    return items


def step_download(api: Api, items: list, out_dir: str) -> list:
    ink = api.ink
    heading(ink, "5", "GET /v1/artifacts/{id}/content -- download every one of them")
    os.makedirs(out_dir, exist_ok=True)
    print("    Not only the video. Each file is hashed here and the hash is compared with the")
    print("    ETag the server sent, which is the server's own record of the stored bytes.")

    taken: set = set()
    downloads = []

    for item in items:
        artifact_id = item["artifact_id"]
        role = item.get("role", "UNKNOWN")
        print()
        reply = api.send("GET", f"/v1/artifacts/{artifact_id}/content")
        if reply.status != 200:
            raise DemoFailure(f"downloading the {role} artifact failed with HTTP {reply.status}")

        media_type = reply.content_type.split(";")[0].strip() or item.get("media_type", "")
        path = os.path.join(out_dir, filename_for(role, media_type, taken))
        with open(path, "wb") as handle:
            handle.write(reply.body)

        digest = hashlib.sha256(reply.body).hexdigest()
        etag = reply.header("ETag")
        claimed = etag_digest(etag)
        verified = claimed == digest

        print(f"    wrote      {os.path.abspath(path)}")
        print(f"    bytes      {len(reply.body)}")
        print(f"    ETag       {etag or '(none)'}")
        print(f"    sha256     {digest}")
        if claimed is None:
            print(ink.yellow("    verdict    the ETag is not a sha256, nothing to compare"))
        elif verified:
            print(ink.green("    verdict    MATCH -- the bytes are what the ETag says they are"))
        else:
            raise DemoFailure(
                f"the {role} download does not match its own ETag ({digest} vs {claimed})"
            )

        downloads.append(
            Download(
                role=role,
                media_type=media_type,
                artifact_id=artifact_id,
                path=path,
                size_bytes=len(reply.body),
                sha256=digest,
                etag=etag,
                verified=verified,
            )
        )
    return downloads


def step_summary(api: Api, job_id: str, document: dict, downloads: list) -> None:
    ink = api.ink
    heading(ink, "6", "what the learner asked for, and what the learner got")
    print(f"    job id       {job_id}")
    print(f"    status       {document.get('status')} / {document.get('stage')}")
    print(f"    took         {document.get('_elapsed_s', 0.0):.1f}s")
    print(f"    profile      {document.get('profile')}")
    print()
    print(ink.bold(f"    {'ROLE':<12}{'BYTES':>12}  FILE"))
    for one in downloads:
        print(f"    {one.role:<12}{one.size_bytes:>12}  {os.path.abspath(one.path)}")
    print()

    primary = next((one for one in downloads if one.role == "PRIMARY"), None)
    if primary is None:
        raise DemoFailure("nothing with role PRIMARY reached the disk")

    print(ink.bold("    the proof: is the downloaded video a lesson this repository committed?"))
    if not os.path.isdir(FIXTURE_DIR):
        # Pointed at a remote host, or run from outside a checkout. The download worked and
        # there is nothing here to compare it against; saying so is the only honest answer.
        note(ink, f"no fixture directory at {FIXTURE_DIR}.")
        note(ink, "the video downloaded, bytes NOT compared against a fixture.")
        return

    compared = 0
    for lesson in FIXTURE_LESSONS:
        candidate = os.path.join(FIXTURE_DIR, lesson)
        if not os.path.isfile(candidate):
            continue
        compared += 1
        if sha256_of_file(candidate) == primary.sha256:
            print()
            print(
                ink.green(
                    f"    MATCH. {os.path.basename(primary.path)} is byte for byte "
                    f"src/app/generation/backends/fixtures/{lesson}."
                )
            )
            print(ink.green("    A job submitted over HTTP came back as that video."))
            return

    if compared == 0:
        note(ink, f"found no lesson fixtures in {FIXTURE_DIR}.")
        note(ink, "the video downloaded, bytes NOT compared against a fixture.")
        return

    raise DemoFailure(
        "the video downloaded but is neither committed lesson -- the request path worked and "
        "what it served is not a file the mock backend holds"
    )


def step_stranger(api: Api, job_id: str) -> None:
    ink = api.ink
    heading(ink, "7", "GET /v1/jobs/{id} under a different X-User-Id -- the expected 200")
    stranger = Api(api.base_url, "u_not_the_submitter", ink)
    print("    Same job id as step 1, a caller who did not submit it. The answer is 200, and")
    print("    that is a decision rather than an oversight (D111): with no authentication, the")
    print("    job id is the only thing a caller can present, so it is what the read checks.")
    print("    An ownership predicate over an unverified header would refuse nobody anyway --")
    print(ink.bold("    somebody after another learner's job would send that learner's id."))
    print()
    reply = stranger.send("GET", f"/v1/jobs/{job_id}")
    if reply.status != 200:
        raise DemoFailure(
            f"a second caller reading a real job answered {reply.status}, expected 200 (D111)"
        )
    note(ink, "@audit the absence of authentication is real and this step is what it looks like.")
    note(ink, "It is the line to change before a deployment, not before a demo.")


def step_missing_job(api: Api) -> None:
    ink = api.ink
    heading(ink, "8", "GET /v1/jobs/{id} for a job that does not exist -- the expected 404")
    print("    The id below is well formed, so it reaches the service rather than bouncing off")
    print("    the router as a 400. There is no such job, so the answer is a 404 inside the")
    print(ink.bold("    same error envelope every failure uses. This 404 is the expected result."))
    print()
    reply = api.send("GET", f"/v1/jobs/{ABSENT_JOB_ID}")
    if reply.status != 404:
        raise DemoFailure(
            f"reading a job that does not exist answered {reply.status}, expected 404"
        )
    note(ink, "404 as expected. Reading a job is open to any caller holding the id, by design,")
    note(ink, "so a wrong caller is not what produces this -- a wrong id is. Step 7 is that.")


def step_failed_job(api: Api, poll_seconds: float, poll_timeout: float) -> None:
    ink = api.ink
    heading(ink, "9", "a job that fails -- FAILED on the job, 200 on the request")
    print("    FAIL_ME anywhere in the instruction makes the mock backend report a failed run")
    print("    (D107). Everything else about the submit is ordinary, so this walks the same")
    print("    pipeline and ends somewhere else.")
    print()

    job_id = None
    reply = api.send("POST", "/v1/jobs", {"instruction": "FAIL_ME please break on purpose"})
    if reply.status == 429:
        raise DemoFailure(
            "the failure step could not submit: this caller already has three jobs in flight "
            "(D090). Run --full on its own, or pass a different --user-id"
        )
    if reply.status not in (200, 201, 202):
        raise DemoFailure(f"the failing submit answered HTTP {reply.status}")
    job_id = (reply.json() or {}).get("job_id")
    if not job_id:
        raise DemoFailure("the failing submit returned no job_id")

    document = poll_until_terminal(api, job_id, poll_seconds, poll_timeout)
    status = str(document.get("status"))
    failure = document.get("failure") or {}
    code = failure.get("code")

    print()
    if status != "FAILED":
        raise DemoFailure(f"a FAIL_ME job ended {status}, expected FAILED")
    if code != "GENERATION_FAILED":
        raise DemoFailure(
            f"the failed job carries failure.code {code!r}, expected GENERATION_FAILED"
        )

    print(
        ink.green(f"    status {status}, failure.code {code}, and every poll above answered 200.")
    )
    print("    That last part is the point. The job failed; the request to read the job did not,")
    print("    so the failure arrives in the document as data rather than as an HTTP status.")
    note(
        ink, "artifact is null on a failed job: nothing was published, so there is nothing to get."
    )


def drain(api: Api, job_ids: list, poll_seconds: float, poll_timeout: float) -> None:
    """Wait for jobs a step started for its own reasons, quietly, one line each at the end.

    Not politeness. The worker is single-flight on purpose (`work_max_concurrent_jobs` is 1,
    and `worker.py` warns if it is set to anything else), so three renders left running are up
    to three minutes sitting in front of whatever anybody runs next -- and the thing they
    usually run next is `scripts/demo.sh`, which gives up after 60 polls. A step that walks away
    from its own jobs does not save that time, it spends somebody else's.
    """
    ink = api.ink
    for job_id in job_ids:
        started = time.monotonic()
        while True:
            reply = api.send("GET", f"/v1/jobs/{job_id}", announce=False)
            if reply.status != 200:
                raise DemoFailure(f"draining {job_id} answered HTTP {reply.status}")
            status = str((reply.json() or {}).get("status", "?"))
            elapsed = time.monotonic() - started
            if status in TERMINAL_STATUSES:
                print(f"    drained {job_id} -> {status} after {elapsed:.1f}s")
                break
            if elapsed > poll_timeout:
                raise DemoFailure(f"draining {job_id} gave up with it still {status}")
            time.sleep(poll_seconds)
    print(ink.dim("    the queue is empty again, so the next run starts from where this one did"))


def step_admission_cap(api: Api, poll_seconds: float, poll_timeout: float) -> None:
    ink = api.ink
    heading(ink, "10", "four submits from one caller -- the expected 429 on the fourth")
    # A caller of this step's own, stamped with the clock. The count is per principal, so
    # borrowing --user-id would make the answer depend on what that caller happened to have left
    # running, and the whole point of the step is that the fourth submit is refused for a reason
    # the step created itself.
    loud = Api(api.base_url, f"u_cap_{int(time.time())}", ink)
    print(f"    A caller minted for this step: {loud.user_id}")
    print("    Admission is one indexed count before anything is inserted: three jobs already")
    print("    QUEUED or RUNNING for a principal and the fourth submit is refused (D090). It is")
    print("    not a rate limit and it is not authorisation -- it is the answer to whether this")
    print("    service can take another job right now.")
    print()

    accepted = []
    for attempt in range(1, 5):
        reply = loud.send("POST", "/v1/jobs", {"instruction": f"cap probe {attempt}"})
        expected = 429 if attempt == 4 else 202
        if reply.status != expected:
            raise DemoFailure(
                f"submit {attempt} of 4 answered {reply.status}, expected {expected} -- the "
                "admission cap is not behaving as D090 describes"
            )
        print(ink.bold(f"    submit {attempt} of 4 -> {reply.status}"))
        if reply.status == 202:
            accepted.append((reply.json() or {}).get("job_id"))

    print()
    print(ink.green("    The fourth was refused with TOO_MANY_ACTIVE_JOBS, in the same error"))
    print(ink.green("    envelope as every other failure."))
    print()
    print("    Now the three that were accepted are drained, because the worker takes one job")
    print("    at a time and leaving them queued would make the next run of anything wait.")
    print("    This is most of what --full costs.")
    drain(loud, [one for one in accepted if one], poll_seconds, poll_timeout)


# --------------------------------------------------------------------------------- the driver


def parse_args(argv: list | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Walk the whole /v1 API against a running stack and print what happens.",
    )
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--user-id", default="u_demo")
    parser.add_argument("--out-dir", default="./demo-out")
    parser.add_argument("--instruction", default="why do atoms form covalent bonds")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--poll-timeout", type=float, default=300.0)
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument(
        "--full",
        action="store_true",
        help=(
            "add steps 9 and 10: a FAIL_ME job polled to FAILED, and four submits that provoke "
            "429 TOO_MANY_ACTIVE_JOBS. Both need jobs of their own and the worker runs one at a "
            "time, so this costs four more renders and roughly triples the wall clock"
        ),
    )
    return parser.parse_args(argv)


def use_color(no_color: bool) -> bool:
    if no_color or os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def run(args: argparse.Namespace, ink: Ink) -> None:
    api = Api(args.base_url, args.user_id, ink)
    print(ink.bold("growtrics api walkthrough"))
    print(f"    base url  {api.base_url}")
    print(f"    caller    {api.user_id}  (sent as X-User-Id on every call)")
    print(f"    out dir   {os.path.abspath(args.out_dir)}")

    step_health(api)
    job_id = step_submit(api, args.instruction)
    document = step_poll(api, job_id, args.poll_seconds, args.poll_timeout)
    step_list_jobs(api)
    items = step_list_artifacts(api, job_id)
    downloads = step_download(api, items, args.out_dir)
    step_summary(api, job_id, document, downloads)
    step_stranger(api, job_id)
    step_missing_job(api)

    if args.full:
        step_failed_job(api, args.poll_seconds, args.poll_timeout)
        step_admission_cap(api, args.poll_seconds, args.poll_timeout)

    print()
    print(ink.green(RULE))
    print(ink.green("done. the pipeline ran end to end and the video is on disk."))
    print(ink.green(RULE))


def main(argv: list | None = None) -> int:
    args = parse_args(argv)
    ink = Ink(use_color(args.no_color))
    try:
        run(args, ink)
    except DemoFailure as failure:
        print()
        print(ink.red(f"api_demo: {failure}"), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
        print(ink.red("api_demo: interrupted"), file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
