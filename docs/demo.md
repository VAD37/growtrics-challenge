# Demo, draft 1

**Approved. This is what gets built** (D085, D100). Design lives in `plan/`; this page is the
scope, and it wins over any wider version of the same thing.

`plan/13-mvp.md` still describes ten stages, eleven tables, and twelve endpoints. This doc is the
smaller thing that actually gets built, taken directly from the reviewer's success test:

1. A user sends a query: their id, an instruction, some context.
2. The server answers with a job id, or refuses because it cannot take another job.
3. The user polls job status until it is done, and the status says whether an artifact exists.
4. The user queries artifacts.
5. The user gets a video.

Plus one thing outside those five: the user can list all of their jobs and all of their
artifacts.

Nothing else is in. Cost metering, budgets, the event feed, metrics, and the operator views are
out of the demo entirely, not stubbed. They were designed in `plan/05`, `plan/08`, and
`plan/13`, and every one of them comes back as an additive change.

`plan/13-mvp.md` stays as written. It is the superset the demo is a subset of, and the frozen
schema and contract still live there. This doc records what is cut and what each cut costs to
restore.

## Surface

Six endpoints.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | database reachable |
| POST | `/v1/jobs` | step 1 and 2 |
| GET | `/v1/jobs/{job_id}` | step 3 |
| GET | `/v1/jobs` | all of the caller's jobs, newest first |
| GET | `/v1/artifacts` | all of the caller's artifacts, optional `?job_id=` filter |
| GET | `/v1/artifacts/{artifact_id}/content` | step 5, bytes |

`GET /v1/artifacts` covers both "everything I have made" and "what did this job produce",
so step 4 costs one endpoint rather than two. `/v1/contexts/{ctx}/artifacts` keeps its frozen
meaning and is not built, because the demo has no chat contexts.

### Identity

`X-User-Id: u_demo` on every request. The auth stub resolves it to a `Principal`, then to an
`AccessScope`, and nothing downstream knows the difference between this and a real token. The
header sits at the credential position rather than in the body, so a request still cannot name
whose job it is, and swapping in `Authorization: Bearer` later changes one function (D086).

`@audit` `X-User-Id` is unauthenticated impersonation by design. Any caller can be any user.
This is acceptable only because the demo has no real data in it, and it is the single line that
has to change before anything is deployed anywhere real.

### Headers and admission

`Idempotency-Key` becomes optional (D089). When present it behaves as designed: same key and
same body returns the same job, different body is `409`. When absent the server mints one, so
that call simply has no replay protection. The derivation spine is unchanged, and it now reads:

```
job_id = uuid5(NS_JOB, principal_id | chat_context_id or "" | idempotency_key)
```

Submit runs one admission check before inserting anything: a principal with 3 jobs already in
`QUEUED` or `RUNNING` gets `429 TOO_MANY_ACTIVE_JOBS` (D090). That is what "if it can handle a
new job" means here, it is one indexed count, and it is the only abuse control left standing
after cost metering was cut.

### Request

`CreateJobRequest` from `plan/14-api-schema.md`, minus one field:

```python
class CreateJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    instruction: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    context: Annotated[list[ContextItemIn], Field(max_length=8)] = Field(default_factory=list)
    options: JobOptionsIn = Field(default_factory=JobOptionsIn)
    chat_context_id: ChatContextId | None = None
```

`chat_context_id` is now optional everywhere: on the request, on `jobs`, and on `artifacts`
(D087). A job submitted straight at the API has no conversation to belong to, and pretending
otherwise meant a fake context id in every curl.

### Job document

`JobView` from `plan/14-api-schema.md`, minus `cost`, with `links.events` and `links.deliverable`
gone (D091). Both return additively when their stages are built.

```jsonc
{
  "job_id": "job_...", "chat_context_id": null,
  "status": "RUNNING", "stage": "GENERATING", "attempt": 0,
  "progress": {"percent": 60, "step": "generate", "message": "rendering"},
  "profile": "video.short.v1", "contract_version": "v1",
  "constraints": {"max_duration_s": 90, "language": "en", "reading_level": null},
  "failure": null,
  "artifact": null,
  "created_at": "2026-08-05T09:12:03Z", "updated_at": "2026-08-05T09:12:41Z",
  "links": {"self": "/v1/jobs/job_...", "artifacts": "/v1/artifacts?job_id=job_..."}
}
```

`progress.percent` comes from a static stage-to-percent map, not from step records (D092):
`INTAKE 10, PREPARING 25, GENERATING 60, COLLECTING 80, VERIFYING 90, PUBLISHING 95, DONE 100`.
Monotonic, because the stage order is, and it costs no table.

When the job succeeds, `artifact` holds the primary as an `ArtifactSummaryView` and the client
has its `content_url` without a second call. That is the whole of step 3's "and related
information like artifacts have or not".

### Errors

The demo needs eight codes. `ERROR_CATALOG` owns the message; no message is written at a raise
site (D062).

| Code | HTTP |
|------|------|
| `INVALID_REQUEST` | 400 |
| `UNAUTHENTICATED` | 401 |
| `JOB_NOT_FOUND` | 404 |
| `ARTIFACT_NOT_FOUND` | 404 |
| `IDEMPOTENCY_CONFLICT` | 409 |
| `ARTIFACT_NOT_READY` | 409 |
| `TOO_MANY_ACTIVE_JOBS` | 429 |
| `GENERATION_FAILED` | job failure code, not an HTTP status |

A job that fails puts `GENERATION_FAILED` in `failure.code` and still answers `200` on
`GET /v1/jobs/{id}`. The request to read a failed job did not fail.

## Tables

Six of the eleven, with their frozen DDL from `plan/13-mvp.md` and two amendments below.

| Table | Why it is in |
|-------|--------------|
| `principals` | upserted on first `X-User-Id`, gives every other table a foreign key |
| `briefs` | the intermediary product: what we asked for, not what the user typed |
| `idempotency_keys` | replay of `POST /v1/jobs` |
| `jobs` | the thing the demo is about |
| `work_items` | the queue, `FOR UPDATE SKIP LOCKED` |
| `artifacts` | the output rows |

Not created in migration 1, DDL unchanged and waiting (D093): `chat_context_grants`,
`workflow_runs`, `step_records`, `job_events`, `cost_entries`.

Adding a table later is a migration nobody has to coordinate with a client. Changing what a
column means is the expensive one, which is why the six above keep every frozen column even
where the demo never writes it: `jobs.budget` stays and stays empty, `jobs.brief_id` fills,
`jobs.attempt` stays at 0.

### Amendments to the frozen schema

**A5. `jobs.chat_context_id` and `artifacts.chat_context_id` become nullable** (D087). Both were
`NOT NULL`. Nullable now is cheaper than a backfill later, and a null is the honest value for a
job that arrived without a conversation.

**A6. `artifacts.principal_id text NOT NULL` added** (D088), copied at insert by `custody`, the
same rule that D073 applied to `chat_context_id`. Listing a learner's artifacts becomes one
index scan and stays correct whether or not a chat context exists.

```sql
CREATE INDEX artifacts_by_principal ON artifacts (principal_id, created_at DESC)
    WHERE scan_verdict = 'CLEAN' AND audience = 'LEARNER';
```

D073's `artifacts_by_context` index is still created and simply matches nothing until chat
contexts arrive.

## Stages

Four. Each one ends in something a reviewer can run.

### D0. Types and migration

- [x] `domain/`: id types with prefixes, `JobStatus`, `StageName`, `ArtifactRole`, `Audience`
- [x] `domain/errors.py`: the eight codes above plus `ERROR_CATALOG`
- [x] `uuid5` derivation helpers with fixed namespaces, unit tested
- [x] Pydantic request and response models for the six endpoints
- [x] `OutputContract` registry with `video.short.v1` only
- [x] Migration 1: the six tables, their indexes, A5 and A6 applied

Done when: `uv run pytest tests/unit` passes with no application code in the repo.

### D1. Submit and read

- [x] `infra/Dockerfile`, `docker-compose.yml`: db, storage, api, worker, all four healthy from
      an empty volume (D094)
- [x] `GET /health`, reports database reachability through `SqlDatabaseProbe` (D108)
- [x] Auth stub: `X-User-Id` to `Principal` to `AccessScope`, principal upserted
- [x] SQL repositories for `principals`, `requests`, `briefs`, `jobs` and `artifacts`, plus the
      contract suite over both backends (D106). No `idempotency_keys`: the scope override removed
      replay protection, and `requests` is the row that took its place
- [x] `POST /v1/jobs`: validate, admission check, derive `job_id`, insert job and work item in
      one transaction, `202`
- [x] `GET /v1/jobs/{job_id}` and `GET /v1/jobs`, both requiring an `AccessScope`

Done when: submitting returns a job id, listing shows it `QUEUED`, a second user id gets `404`
on it, and a fourth concurrent submit gets `429`. The job sits at `QUEUED` forever, which is
correct at this stage.

Two halves of that hold and one does not. Submit, listing and the fourth submit's `429` all
answer as written; a second user id gets `200` and not `404`, which is scope override item 1 and
is stated as a deliberate hole in `orchestration/service.py::QueryJob`.

### D2. The loop turns

- [x] The claim itself: `FOR UPDATE SKIP LOCKED` under a lease, plus heartbeat, release, complete
      and the three reads the sweep needs (D106), claimed by the worker entrypoint (D108)
- [x] `intake`: sanitiser, `SanitisedText`, seal the brief, insert the row, render the file set
- [x] `generation`: port plus `MockGenerationBackend` returning a manifest and a committed
      lesson video, in the shape `plan/15-engine-seam.md` defines (D107)
- [x] `custody`: harvest with the path allowlist, size cap, `ResultValidator` against
      `video.short.v1`, write bytes to object storage, insert the artifact row. @audit most of
      the check chain is still a stub; see `custody/verifier.py`
- [x] Status transitions and the stage-to-percent map, written by `orchestration` only
- [x] Failure path: `failure` populated, status `FAILED`, work item not retried

Done when: a submitted job moves on its own from `QUEUED` to `SUCCEEDED` and `jobs.artifact_id`
is set. It does: `QUEUED/INTAKE 10%` to `RUNNING/GENERATING 60%` to `SUCCEEDED/DONE 100%` while a
client polls, and `FAIL_ME` in the instruction reaches `FAILED` with `GENERATION_FAILED` in
`failure.code` on a `200`.

### D3. Output retrieval

- [x] `GET /v1/artifacts`, scoped, cursor paged, optional `?job_id=`, quarantined rows excluded
- [x] `GET /v1/artifacts/{artifact_id}/content`, scoped, streamed from object storage, `ETag`
- [x] `GET /v1/jobs/{id}` carries the primary artifact summary once succeeded
- [x] `scripts/demo.sh` and `scripts/demo.ps1`: the walkthrough below, runnable, ending in a
      sha256 against the committed lesson rather than a file size
- [x] `scripts/api_demo.py`: the same walkthrough printed call by call, every artifact
      downloaded and checked against its `ETag`, standard library only (D111)
- [x] `tests/integration/pipeline_test.py`: the same walkthrough in one process over the memory
      doubles, in `uv run pytest` on a checkout with no Docker and no Postgres
- [x] `tests/integration/compose_e2e_test.py`: the same walkthrough over HTTP against `make up`,
      marked `docker`, deselected by default, skipped cleanly when nothing answers (D110)

Done when: the script below runs end to end from a clean checkout. It does, and it is now written
down twice rather than done by hand.

Two things the run is honest about. `GET /v1/jobs/{id}` answers `200` to a second user id and not
the `404` D1 asks for -- scope override item 1, stated in `orchestration/service.py::QueryJob` and
pinned by an assertion in both integration tests so closing it flips a test rather than surprising
somebody. And `GET /v1/artifacts` returns POSTER, PRIMARY and TRANSCRIPT ordered by id, so a
client wanting the video reads `role`, which is what `job.artifact` carries for it.

## The demo

`scripts/demo.sh [base-url]` is every call below, in order, with the download hashed at the end.
`scripts/demo.ps1` is its twin. Neither needs `jq`.

```bash
docker compose up -d

# 1, 2
curl -X POST localhost:8000/v1/jobs \
  -H 'X-User-Id: u_demo' -H 'Content-Type: application/json' \
  -d '{"instruction":"why do atoms form covalent bonds",
       "context":[{"kind":"LEVEL","text":"grade 9"}]}'
# -> 202 {"job_id":"job_...","status":"QUEUED","stage":"INTAKE",...}
# -> 429 {"error":{"code":"TOO_MANY_ACTIVE_JOBS",...}} when three are already in flight

# 3
curl -H 'X-User-Id: u_demo' localhost:8000/v1/jobs/job_...
# -> {"status":"RUNNING","stage":"GENERATING","progress":{"percent":60,...},"artifact":null}
# -> {"status":"SUCCEEDED","stage":"DONE","artifact":{"artifact_id":"art_...",
#     "media_type":"video/mp4","content_url":"/v1/artifacts/art_.../content",...}}

curl -H 'X-User-Id: u_demo' localhost:8000/v1/jobs

# 4
curl -H 'X-User-Id: u_demo' 'localhost:8000/v1/artifacts?job_id=job_...'
curl -H 'X-User-Id: u_demo' localhost:8000/v1/artifacts

# 5
curl -H 'X-User-Id: u_demo' -o lesson.mp4 localhost:8000/v1/artifacts/art_.../content

# 6, and this is the step that makes it a proof
sha256sum lesson.mp4 src/app/generation/backends/fixtures/lesson_a.mp4
# -> the same digest twice
```

`art_...` above is the **PRIMARY** row. Step 4 returns three, and the first one in the page is
whichever id sorted lowest.

## What each cut costs to restore

The point of cutting this way rather than by deleting design: none of these needs a schema
change to a table the demo writes.

| Cut | Restoring it |
|-----|--------------|
| Event feed, `GET /v1/jobs/{id}/events` | create `job_events`, write through the outbox, one endpoint |
| Metrics | read from `job_events`; nothing new |
| Cost metering and budgets | create `cost_entries`, add the `cost` block to `JobView`, wrap steps |
| Checkpoint and resume | create `workflow_runs` and `step_records`, engine reads them |
| Sweeper | needs `workflow_runs` only |
| Chat contexts | create `chat_context_grants`, fill the two nullable columns, build the frozen `/v1/contexts/{ctx}/artifacts` |
| Real auth | replace the header resolver, one function |
| Cancel, `/v1/me`, deliverable, `/v1/jobs/{id}/content` | additive endpoints, no schema change |
| Guard, classifier, red-team corpus | inside `intake`, no schema change |
| Real generation | replace `ScriptedBackend` with the engine in `../growtrics-llm-engine`, no schema change (D101) |

## Honest note on what this proves

The demo shows a job system with idempotent submit, scoped reads, an asynchronous worker, and
verified output. It does not show the parts of this design that are actually interesting:
surviving a crashed run, resisting a malicious prompt, and keeping an agent's output contained.
Those are stages 5 to 8 of `plan/13-mvp.md`, and they stay written down rather than built.

The trade is deliberate. A reviewer cannot see failure survival on a system that does not run
yet, and every one of those stages attaches to the same six tables.
