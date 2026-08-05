# API design

REST over a job resource. Submit, poll, watch, fetch. No streaming transport this round;
polling with an event cursor covers observation (N7) without adding a second protocol.

## Surface

| Method | Path | Purpose | Success |
|--------|------|---------|---------|
| POST | `/v1/jobs` | Submit a video generation job | `202` new, `200` idempotent replay |
| GET | `/v1/jobs` | List the caller's jobs, filter by status, cursor paged | `200` |
| GET | `/v1/jobs/{job_id}` | Status, stage, progress, usage, error | `200` |
| GET | `/v1/jobs/{job_id}/events` | Ordered event feed from a cursor | `200` |
| POST | `/v1/jobs/{job_id}/cancel` | Best effort stop | `202` |
| GET | `/v1/jobs/{job_id}/deliverable` | The full artifact set with its primary | `200`, `409` if not ready |
| GET | `/v1/jobs/{job_id}/content` | Redirect to the primary artifact's bytes | `302`, `409` if not ready |
| GET | `/v1/contexts/{chat_context_id}/artifacts` | Artifact history for one conversation | `200` |
| GET | `/v1/artifacts/{artifact_id}/content` | The bytes | `200` or `302` |
| GET | `/v1/me` | Principal, entitlement, balance. Stub | `200` |
| GET | `/health`, `/ready` | Liveness, readiness | `200` |
| GET | `/internal/metrics` | Stub metric registry dump | `200` |

Cancel is listed because the workflow needs a cancellation path anyway for lease teardown.
It is a candidate for the cut list; see `10-scope-matrix.md`.

## Submit

```http
POST /v1/jobs
Idempotency-Key: 3f1c9e0a-...
Content-Type: application/json

{
  "chat_context_id": "ctx_01JA9Z...",
  "instruction": "explain why atoms form covalent bonds",
  "context": [
    {"kind": "LEVEL", "text": "grade 9"},
    {"kind": "PRIOR_TOPIC", "text": "we covered ionic bonding last week"},
    {"kind": "MISCONCEPTION", "text": "thinks electrons are transferred in every bond"}
  ],
  "options": {
    "profile": "video.short.v1",
    "max_duration_s": 90,
    "language": "en"
  }
}
```

The principal comes from the auth stub, not the body. A client cannot name whose job this is.

Field rules, all enforced at the edge:

- `Idempotency-Key`: required. Missing is `400`, not a convenience default. The job id is
  derived from it, so it is the primary key of the whole request, not a retry hint.
- `chat_context_id`: required. Checked against the principal before anything else runs.
- `instruction`: 1 to 500 characters after normalisation. Required.
- `context`: at most 8 items, each at most 500 characters, `kind` from the closed enum in
  `01-domain-model.md`. Optional.
- `options.profile`: a server-owned `ProfileId`, default `video.short.v1`. Unknown or disabled
  is `422 OUTPUT_PROFILE_NOT_SUPPORTED`. A client selects a profile; it never authors an output
  contract.
- `options.max_duration_s`: 15 to 180, and rejected with `400` when outside it. Clamping was the
  earlier rule and is withdrawn: a silently altered request is a silent partial success (N5).
  The effective value, after the profile's own cap, is echoed on the job document.
- Total request body capped at 16 KiB. Larger is `413`, not a truncation.

Full Pydantic types for every model on this page are in `14-api-schema.md`.

Context arrives as typed items rather than one prose blob. That is deliberate: it keeps the
brief renderer from having to guess where one piece of user text ends and another begins, and
it makes each item individually attributable in the audit trail.

### 202 response

```json
{
  "job_id": "job_01JB2K...",
  "chat_context_id": "ctx_01JA9Z...",
  "status": "QUEUED",
  "stage": "INTAKE",
  "attempt": 0,
  "progress": {"percent": 5, "step": "intake", "message": "checking your request"},
  "profile": "video.short.v1",
  "contract_version": "1",
  "constraints": {"max_duration_s": 90, "language": "en", "reading_level": null},
  "cost": {
    "ceiling_micros": 45000,
    "spent_micros": 0,
    "measured_micros": 0,
    "claimed_micros": 0
  },
  "artifact": null,
  "created_at": "2026-08-05T09:12:03Z",
  "links": {
    "self": "/v1/jobs/job_01JB2K...",
    "events": "/v1/jobs/job_01JB2K.../events",
    "deliverable": null,
    "content": null
  }
}
```

`Location: /v1/jobs/{job_id}` and `Retry-After: 5` are set on the `202`.

`cost.measured_micros` and `cost.claimed_micros` are reported separately because they are not
the same kind of number. Measured is what the backend metered. Claimed is what a worker said
it spent, and a worker is untrusted input. See `11-triage.md`.

### Idempotency

The key is required, and the job id is derived from it:

```
job_id = uuid5(NS_JOB, principal_id | chat_context_id | idempotency_key)
```

- Same key, same body digest: return the existing job with `200`. Not a new job, not an error.
- Same key, different body digest: `409 IDEMPOTENCY_KEY_REUSED`. Never a silent alias onto
  someone else's job.
- Missing key: `400`. There is no unkeyed path.

Because the id is derived rather than generated, a duplicate submission collides on the
primary key instead of racing a read-then-write. Deduplication is a database constraint, which
is the only version of it that survives two API nodes running at once.

The same root key derives the run id, the worker session id, every step's external effect key,
and the artifact id. One spine, so at-least-once delivery is safe by construction instead of
by remembering to handle it in each step. Full derivation table in `01-domain-model.md`.

## Poll

`GET /v1/jobs/{job_id}` returns the same document as the submit response, plus `error` and
`artifact` once they exist. Two properties the client can rely on:

- `progress.percent` is monotonic within a job. A retry does not walk it backwards; it bumps
  `attempt` and leaves the percent where it was. A progress bar that goes backwards reads as
  a broken system even when the system is recovering correctly.
- `stage` is a short, stable, human word. Step names may be renamed freely; stage names are
  part of the contract.

Stage values: `INTAKE`, `ADMISSION`, `PLACEMENT`, `PREPARING`, `GENERATING`, `COLLECTING`,
`VERIFYING`, `PUBLISHING`, `DONE`, `FAILED`.

`Retry-After` is returned on any non-terminal job so a client does not have to invent a
polling interval. It grows with the expected remaining work: 2 seconds during intake,
10 seconds during generation.

## Events

```http
GET /v1/jobs/{job_id}/events?since=12&limit=100
```

```json
{
  "job_id": "job_01JB2K...",
  "events": [
    {"seq": 13, "at": "...", "type": "BriefSealed",       "message": "request understood"},
    {"seq": 14, "at": "...", "type": "BudgetHeld",        "message": "reserved 1200 tokens"},
    {"seq": 15, "at": "...", "type": "RenderSessionOpened","message": "building your video"},
    {"seq": 16, "at": "...", "type": "RenderProgressed",  "message": "scene 2 of 5 drafted"}
  ],
  "next_since": 16,
  "complete": false
}
```

Only events marked user-visible appear here. Operator events stay in the log and the metrics
sink. `seq` is per job and dense, so a client can detect a gap.

This endpoint is what satisfies "the user must have observation of their task and progress".
The status document says where the job is; the event feed says how it got there, which is the
part that makes a two-minute wait tolerable.

`@TODO` server-sent events over the same feed is a small addition once the cursor exists. Not
this round.

## Deliverable

A job produces one **deliverable**: a role-keyed set of artifacts with one designated primary.
The job document carries the primary alone, because "what do I play" is the common question;
the set is one link away for a client that renders a full lesson panel. Rationale, and what
happens when the primary is not a video, in `14-api-schema.md`.

```http
GET /v1/jobs/{job_id}/deliverable
```

```json
{
  "job_id": "job_01JB2K...",
  "profile": "video.short.v1",
  "contract_version": "1",
  "status": "READY",
  "primary_artifact_id": "art_01JB2M...",
  "artifacts": [
    {"artifact_id": "art_01JB2M...", "role": "PRIMARY", "media_type": "video/mp4",
     "size_bytes": 4821330, "content_hash": "sha256:9f2b...", "rel_path": null,
     "media": {"duration_s": 74.2, "width": 1280, "height": 720, "has_audio": true},
     "content_url": "/v1/artifacts/art_01JB2M.../content"},
    {"artifact_id": "art_01JB2N...", "role": "TRANSCRIPT", "media_type": "text/plain",
     "size_bytes": 3120, "content_hash": "sha256:41ca...", "rel_path": null, "media": null,
     "content_url": "/v1/artifacts/art_01JB2N.../content"},
    {"artifact_id": "art_01JB2P...", "role": "POSTER", "media_type": "image/png",
     "size_bytes": 91204, "content_hash": "sha256:7d10...", "rel_path": null, "media": null,
     "content_url": "/v1/artifacts/art_01JB2P.../content"}
  ],
  "total_size_bytes": 4915654,
  "verification": {
    "validator_version": "rv-1",
    "verdict": "CLEAN",
    "worker_claim_agreed": true,
    "checks": [{"name": "audio_not_silent", "passed": true}]
  },
  "completed_at": "2026-08-05T09:14:31Z"
}
```

Only `audience = LEARNER` artifacts appear here. A harvested agent log is a row and an operator
can read it; there is no value of any query parameter that puts it in this list.

`409 ARTIFACT_NOT_READY` with the current status if the job has not succeeded. Never a `404`,
which would suggest the job does not exist.

`GET /v1/jobs/{job_id}/content` is the one-request form: `302` to the primary's bytes, so a
walkthrough is `curl -L .../content -o lesson.mp4` rather than two calls and a `jq`.

### Artifact history for a conversation

```http
GET /v1/contexts/{chat_context_id}/artifacts?limit=20&cursor=...
```

Returns the artifacts produced for that conversation, newest first, each with its job id,
status, and content link. This is what the chat panel renders, and what a redirect from a chat
message resolves against.

Defaults to `role=PRIMARY`, so one lesson is one row. Without that default the panel shows a
transcript and a poster as if they were two more lessons. `?role=` widens it; no value of it
reaches an `audience = OPERATOR` row.

The context id is checked against the principal before the query runs. An id that exists but
belongs to someone else returns the same `404` as an id that does not exist.

`@audit` we index by `chat_context_id` and do not own it. If the chat service ever reuses or
recycles a context id, this listing silently mixes conversations. Worth a constraint from
whoever issues the ids.

Content is served from our storage. `Content-Disposition: inline`, byte-range supported,
`ETag` set to the content hash. No route here can resolve to a worker host.

## Errors

Envelope carried over from round 1:

```json
{"error": {"code": "REQUEST_REJECTED", "message": "...", "details": {}}}
```

No message in this table is written at a raise site. `domain/errors.py` holds one enum and one
`ERROR_CATALOG` mapping each code to its HTTP status, its retryable classification, the
learner-facing sentence, and an operator hint. The API layer reads the catalog; it does not
own the mapping. One place to audit for leaks, one place to translate later.

| Code | HTTP | Meaning |
|------|------|---------|
| `INVALID_REQUEST` | 400 | Shape, size, or enum violation |
| `REQUEST_REJECTED` | 422 | Intake guard refused. `details.reason` is a coarse category |
| `SUBJECT_NOT_SUPPORTED` | 422 | Not a learning request we cover |
| `OUTPUT_PROFILE_NOT_SUPPORTED` | 422 | Profile unknown, disabled, or not offered for this concept |
| `IDEMPOTENCY_KEY_REUSED` | 409 | Same key, different body |
| `INSUFFICIENT_BALANCE` | 402 | Stub account has no room |
| `BUDGET_EXHAUSTED` | 402 | A ceiling was hit mid-run: job, chat context, or daily principal |
| `RATE_LIMITED` | 429 | Admission control. `Retry-After` set. `@TODO` |
| `JOB_NOT_FOUND` | 404 | Unknown id, or not the caller's job |
| `ARTIFACT_NOT_READY` | 409 | Job not in a terminal success state |
| `ARTIFACT_QUARANTINED` | 409 | Produced, failed verification, not servable |
| `DELIVERABLE_INCOMPLETE` | 409 | A required part is missing after verification; the job is FAILED and the evidence is kept |
| `GENERATION_UNAVAILABLE` | 503 | No worker could be placed |
| `GENERATION_TIMEOUT` | 504 | Worker exceeded its wall clock |
| `INTERNAL_ERROR` | 500 | Everything else, with a trace id in details |

Guard rejections stay coarse on purpose. Telling a caller exactly which rule matched turns
the error response into a tuning oracle for the next attempt.

`@audit` `JOB_NOT_FOUND` covering both "does not exist" and "not yours" is correct for
avoiding enumeration, but it makes debugging harder. Operators get the real reason in the
log, keyed by trace id.

## What the API does not do

No domain rules in routers. A router validates a schema, resolves a principal, calls one use
case, and maps the result to a status code. Anything that needs to know what a job means
belongs in `orchestration/`. The test for this: the whole submit and poll flow must be
exercisable in a unit test with no HTTP client involved.
