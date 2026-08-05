# Information and access control

Two questions, answered as tables rather than prose: which module is allowed to write which
data, and which actor is allowed to see it.

Small subsystems are drawn out of this doc on purpose. Metrics, the event feed, and the prompt
guard are real (`06`, `08`) and none of them change who owns what. The picture below is API
input, internal modules, retrievable output.

## Actors

| Actor | Who | Reaches the system through |
|-------|-----|----------------------------|
| User | A learner, via the chat frontend | The public API, always scoped to themselves |
| Admin | An operator of this service | The database and logs. No admin API in draft 1 |
| System | The backend's own modules | Ports, in-process |
| Worker | The agent running on a rented machine | Nothing. It receives files and is polled |

The Worker row is the important one. It has no credential, no endpoint, and no row it can
write. Every other design question about containing an agent gets easier once that is true.

`@audit` "Admin is the database" is a deliberate non-decision. It means there is no audited,
least-privilege admin path, and every operator action is a raw SQL statement. Acceptable for a
demo, not for production. Building an admin API before the product exists is the wrong order,
but the gap should be stated rather than discovered.

## Module ownership

Sole writer means exactly one module issues writes to that data. Everyone else goes through
its port and gets a typed result.

| Module | Sole writer of | Reads | Must never touch |
|--------|----------------|-------|------------------|
| `api` | nothing | job views, artifact views, via use cases | any table directly, brief internals, the worker |
| `access` | `principals`, `chat_context_grants` | its own tables | jobs, artifacts, briefs |
| `intake` | `briefs` | the raw request, in memory only | jobs, artifacts, object storage |
| `orchestration` | `jobs`, `workflow_runs`, `step_records`, `work_items`, `job_events`, `cost_entries` | briefs and artifacts by reference | artifact bytes, object storage, the worker directly |
| `custody` | `artifacts`, object storage | jobs by id, for scoping only | job status, briefs |
| `generation` | nothing persistent | the rendered brief bundle | the database, object storage, principal data |
| `storage` | nothing semantic | whatever its port is asked for | domain rules |

Two consequences worth stating out loud:

**Only `orchestration` writes job status.** Custody cannot mark a job succeeded; it returns a
verified artifact and orchestration decides what that means. This is what keeps invariant 1 of
`VideoJob` checkable in one place.

**Only `custody` writes bytes.** Nothing else opens the object store. The path from an agent's
output to a learner's download has exactly one module on it.

## Data, by actor

R is read, W is write, blank is no access at all.

| Data | User (own) | User (other) | Admin | Worker |
|------|-----------|--------------|-------|--------|
| Job status, stage, progress | R | | R | |
| Job failure code and learner message | R | | R | |
| Job failure operator detail, step records | | | R | |
| Brief: their own instruction, echoed back | R | | R | |
| Brief: rendered files, guard verdict, template version | | | R | receives files, cannot read back |
| Artifact metadata for their own job | R | | R | |
| Artifact bytes for their own job | R | | R | writes once, by being harvested |
| Quarantined artifact, anything | | | R | |
| Cost: their own job total | R | | R | |
| Cost: per-step entries, measured against claimed | | | R | |
| Any other principal's anything | | | R | |
| Provider keys, storage credentials, signing keys | | | | |

The last row has no R anywhere. Secrets are not admin-readable through this system either;
they live in the deployment's secret store, and the application reads them at startup.

## Where authorisation actually happens

Four checks, all in the use case layer. Not in the router, which would make them skippable by
adding a second router, and not in the repository, which would make them invisible.

1. `resolve_principal(request)` at the edge. Stub for now, returns a `Principal`.
2. `scope_for(principal)` returns an `AccessScope`. This is the only way to obtain one.
3. `assert_context_access(scope, chat_context_id)` before anything scoped to a conversation.
4. `assert_job_access(scope, job_id)` before any job or artifact read.

`AccessScope` uses the same trick as `SanitisedText`: it cannot be constructed outside the
`access` module, and **every repository read method requires one**.

```python
async def list_jobs(self, scope: AccessScope, *, chat_context_id: ChatContextId | None,
                    status: JobStatus | None, cursor: Cursor | None) -> Page[VideoJob]: ...
```

An unscoped query is then not a review finding, it is a missing argument. This matters more
than it looks: the most common way a multi-user system leaks is a listing endpoint that
forgot its `WHERE principal_id = ...`, and no amount of care at the router prevents it
forever.

Chat context ownership is claim-on-first-use in the stub: the first principal to submit a job
against a context id owns it, recorded in `chat_context_grants`. A second principal using the
same id gets the same `404` as an id that does not exist.

`@audit` claim-on-first-use is guessable if context ids are sequential or short. It is safe
only while ids are unguessable random strings. Whoever mints them owes us that guarantee
(Q-W).

## What crosses to the worker

The narrowest seam in the system, so it gets its own list.

| Sent | Not sent |
|------|----------|
| `session_id`, `trace_id` | `job_id`, `run_id`, `principal_id`, `chat_context_id` |
| Sanitised instruction, as fenced data | Anything the user did not write |
| Typed context items, as labelled sections | Any other job, any other learner's data |
| Constraints and the output contract | Balances, costs, entitlements |
| The pinned template repo reference | Any credential of ours |

The worker never learns our job identity. `session_id` is derived from `run_id` by a one-way
hash, so a worker holding it cannot address anything else in the system, and there is nothing
to address it with anyway.

## Redrawn: API call to output

Metrics, event feed, and guard collapsed into the modules that own them. Six boxes.

```
  CLIENT                                                          CLIENT
    │ POST /v1/jobs                              GET /v1/jobs/{id}  ▲
    │ {chat_context_id, instruction, context[]}  GET /v1/contexts/  │
    │ Idempotency-Key: required                      {id}/artifacts │
    ▼                                            GET /v1/artifacts/ │
┌───────────────────────────────────────────────      {id}/content ─┤
│ api          shape validation only, no domain rules               │
└───┬───────────────────────────────────────────────────────────────┘
    │ resolve_principal -> AccessScope
    ▼
┌───────────────────────────────────────────────────────────────────┐
│ access       who is this, may they touch this chat context        │
└───┬───────────────────────────────────────────────────────────────┘
    │ SubmitJob(scope, raw_request, idempotency_key)
    ▼
┌───────────────────────────────────────────────────────────────────┐
│ orchestration    job_id = uuid5(principal|context|idem_key)       │
│                  INSERT jobs + work_items  ── one transaction     │
└───┬───────────────────────────────────────────────────────────────┘
    │ 202 {job_id, status: QUEUED} ─────────────────────────────────▶ CLIENT
    │
    │ ······ worker process claims the work item ······
    │
    ▼
┌───────────────────────────────────────────────────────────────────┐
│ intake       sanitise -> seal LessonBrief -> render BriefBundle   │
│              writes: briefs                                       │
└───┬───────────────────────────────────────────────────────────────┘
    │ BriefBundle (files, no prompt role)
    ▼
┌───────────────────────────────────────────────────────────────────┐
│ generation   acquire worker -> open session -> poll -> outcome    │
│              writes: nothing                                      │
└───┬───────────────────────────────────────────────────────────────┘
    │ ArtifactDescriptor[]  (a claim, not evidence)
    ▼
┌───────────────────────────────────────────────────────────────────┐
│ custody      harvest bytes -> validate -> store -> row            │
│              writes: artifacts, object storage                    │
└───┬───────────────────────────────────────────────────────────────┘
    │ artifact_id
    ▼
┌───────────────────────────────────────────────────────────────────┐
│ orchestration    status = SUCCEEDED, artifact_ref = artifact_id   │
└───────────────────────────────────────────────────────────────────┘
```

Reading it as data rather than boxes:

| Hop | Payload | Trust | Who may see it |
|-----|---------|-------|----------------|
| client to api | `CreateJobRequest` | untrusted | user, admin |
| api to orchestration | `SubmitJob(scope, raw, key)` | untrusted, scoped | system |
| orchestration to intake | `RawLessonRequest` | untrusted | system |
| intake to orchestration | `LessonBrief` + verdict | sealed | admin, system |
| intake to generation | `BriefBundle` files | sealed | worker, admin, system |
| generation to custody | `ArtifactDescriptor[]` + byte streams | untrusted | system |
| custody to orchestration | `Artifact` | domain | user (own), admin |
| api to client | `JobView`, `ArtifactView`, bytes | domain | user (own), admin |

The shape of that column is the whole security story: untrusted going in, sealed in the
middle, untrusted coming back, domain only at the two ends where a person is looking.

## Drawn out of this picture

Present in the design, absent from the diagram because they do not change ownership:
the prompt guard (inside `intake`), job events and metrics (written by `orchestration`,
read-only everywhere else), cost metering (a precondition and a debit around each step), the
sweeper (a second entrypoint on `orchestration`), and the degradation ladder.
