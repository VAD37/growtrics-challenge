# MVP

**Superseded as the build order by `15-demo-cut.md` (D085).** This page stays as the superset:
the frozen SQL schema, the frozen `/v1` surface, and the ten stages the demo cut is a subset of.
Read this for what a table or an endpoint means, and `15-demo-cut.md` for what gets built.

## Definition of done

A reviewer runs one command, then:

1. Calls the API with an instruction and a chat context.
2. Polls and sees the job move.
3. Sees the job finish.
4. Queries the conversation and gets a list of artifacts.
5. Downloads one.

Anything not on the path from 1 to 5 is not MVP. That is the whole test.

```bash
docker compose up -d

# 1
curl -X POST localhost:8000/v1/jobs \
  -H 'Idempotency-Key: demo-001' -H 'Content-Type: application/json' \
  -d '{"chat_context_id":"ctx_demo","instruction":"why do atoms form covalent bonds",
       "context":[{"kind":"LEVEL","text":"grade 9"}]}'
# -> 202 {"job_id":"job_...","status":"QUEUED"}

# 2, 3
curl localhost:8000/v1/jobs/job_...
# -> {"status":"RUNNING","stage":"GENERATING","progress":{"percent":55,...}}
# -> {"status":"SUCCEEDED","artifact":{"artifact_id":"art_...",...}}

# 4
curl localhost:8000/v1/contexts/ctx_demo/artifacts

# 5
curl -o out.mp4 localhost:8000/v1/artifacts/art_.../content
```

## What must be right the first time

Two things are expensive to change once anything depends on them, so they get decided now and
frozen: the SQL schema and the API contract. Everything else in the plan can be rewritten
behind a port without a migration or a client change.

### Upgradability rules

Applied to both, so that today's demo shapes survive into production rather than being thrown
away.

**Identifiers.** Opaque prefixed strings (`job_`, `art_`, `ctx_`, `run_`). Never an integer,
never a sequence. A client that treats an id as opaque cannot be broken by changing how it is
generated, and prefixes make a wrong id obvious in a log.

**Enums are text, in both the database and JSON.** Never an integer whose meaning lives in
application code. New members may be added; existing members are never repurposed or removed,
only deprecated. Clients must tolerate an unknown member rather than crash.

**Typed columns for what is stable, JSONB for what is not.** Status, ids, timestamps, and
counts get real columns and real indexes. Guard verdicts, media probes, request options,
failure detail, and step output are JSONB, because their shape will change and none of them is
queried in a hot path.

**Every row carries `created_at`, `updated_at`, and mutable aggregates carry `version`.**
Optimistic concurrency and audit both need it, and adding it later means backfilling.

**Money as integer micros, durations as explicit units.** `amount_micros`, `duration_s`,
`timeout_ms`. Never a float for money, never a bare number for a duration.

**Timestamps are `timestamptz`, serialised RFC 3339 with `Z`.** No local time anywhere.

**API changes are additive only within `/v1`.** New fields are optional. Removing a field or
changing its type is `/v2`. Responses are always objects, never bare arrays, so a list can
grow a `next_cursor` without breaking a parser.

**Pagination is an opaque cursor, not an offset.** Offsets skip and duplicate rows under
concurrent inserts, which a job list gets constantly.

**Nothing references a worker.** No foreign key, no stored host, no session table. Workers are
ephemeral and external; a session id is a derived value, not a row.

## Frozen: SQL schema

Postgres. The MVP populates a subset; the tables exist from the first migration so the shape
does not churn.

```sql
-- access ---------------------------------------------------------------
CREATE TABLE principals (
    principal_id     text PRIMARY KEY,
    external_id      text UNIQUE NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE chat_context_grants (
    chat_context_id  text PRIMARY KEY,           -- foreign id, we index and do not own
    principal_id     text NOT NULL REFERENCES principals,
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- intake ---------------------------------------------------------------
CREATE TABLE briefs (
    brief_id         text PRIMARY KEY,
    job_id           text NOT NULL,
    brief_hash       text NOT NULL,
    template_version text NOT NULL,
    subject          text NOT NULL,
    concept_id       text,
    instruction      text NOT NULL,              -- sanitised
    context_items    jsonb NOT NULL DEFAULT '[]',
    constraints      jsonb NOT NULL DEFAULT '{}',
    guard_verdict    jsonb NOT NULL DEFAULT '{}',
    sealed_at        timestamptz NOT NULL DEFAULT now()
);

-- orchestration --------------------------------------------------------
CREATE TABLE idempotency_keys (
    principal_id     text NOT NULL REFERENCES principals,
    idempotency_key  text NOT NULL,
    request_digest   text NOT NULL,
    job_id           text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (principal_id, idempotency_key)
);

CREATE TABLE jobs (
    job_id           text PRIMARY KEY,           -- uuid5, derived. See 01-domain-model.md
    principal_id     text NOT NULL REFERENCES principals,
    chat_context_id  text NOT NULL,
    idempotency_key  text NOT NULL,
    request_digest   text NOT NULL,
    brief_id         text,
    status           text NOT NULL,              -- QUEUED RUNNING SUCCEEDED FAILED CANCELLED
    stage            text NOT NULL,
    attempt          int  NOT NULL DEFAULT 0,
    progress_percent int  NOT NULL DEFAULT 0,
    profile          text NOT NULL DEFAULT 'video.short.v1',   -- what to make
    output_contract  text NOT NULL DEFAULT 'v1',               -- under which rules
    artifact_id      text,                                     -- the PRIMARY artifact
    budget           jsonb NOT NULL DEFAULT '{}',
    failure          jsonb,                      -- {code, stage, message}
    version          int  NOT NULL DEFAULT 0,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX jobs_by_principal ON jobs (principal_id, created_at DESC);
CREATE INDEX jobs_by_context   ON jobs (chat_context_id, created_at DESC);
CREATE INDEX jobs_active       ON jobs (status) WHERE status IN ('QUEUED','RUNNING');

CREATE TABLE workflow_runs (
    run_id           text PRIMARY KEY,
    job_id           text NOT NULL REFERENCES jobs,
    workflow_version text NOT NULL,
    run_ordinal      int  NOT NULL,
    cursor_step      text,
    lease_owner      text,
    lease_expires_at timestamptz,
    outcome          text,
    started_at       timestamptz NOT NULL DEFAULT now(),
    ended_at         timestamptz
);
CREATE INDEX runs_expired_leases ON workflow_runs (lease_expires_at)
    WHERE outcome IS NULL;

CREATE TABLE step_records (
    run_id           text NOT NULL REFERENCES workflow_runs,
    step_name        text NOT NULL,
    attempt          int  NOT NULL,
    status           text NOT NULL,
    input_digest     text NOT NULL,
    output           jsonb,
    error            jsonb,
    started_at       timestamptz NOT NULL DEFAULT now(),
    ended_at         timestamptz,
    PRIMARY KEY (run_id, step_name)
);

CREATE TABLE work_items (
    item_id          text PRIMARY KEY,
    job_id           text NOT NULL REFERENCES jobs,
    run_id           text,
    available_at     timestamptz NOT NULL DEFAULT now(),
    claimed_by       text,
    claimed_until    timestamptz,
    claim_count      int NOT NULL DEFAULT 0,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX work_items_claimable ON work_items (available_at)
    WHERE claimed_by IS NULL;

CREATE TABLE job_events (
    job_id           text NOT NULL REFERENCES jobs,
    seq              bigint NOT NULL,
    at               timestamptz NOT NULL DEFAULT now(),
    type             text NOT NULL,
    stage            text NOT NULL,
    attempt          int  NOT NULL DEFAULT 0,
    severity         text NOT NULL DEFAULT 'INFO',
    visibility       text NOT NULL DEFAULT 'OPERATOR',   -- USER | OPERATOR
    message          text NOT NULL,
    detail           jsonb NOT NULL DEFAULT '{}',        -- never serialised to a client
    trace_id         text NOT NULL,
    PRIMARY KEY (job_id, seq)
);

CREATE TABLE cost_entries (
    entry_id         text PRIMARY KEY,
    job_id           text NOT NULL REFERENCES jobs,
    run_id           text,
    step_name        text NOT NULL,
    attempt          int NOT NULL DEFAULT 0,
    model            text,
    input_tokens     bigint NOT NULL DEFAULT 0,
    output_tokens    bigint NOT NULL DEFAULT 0,
    unit_price_version text NOT NULL,
    amount_micros    bigint NOT NULL DEFAULT 0,
    source           text NOT NULL,              -- MEASURED | CLAIMED
    at               timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX cost_by_job ON cost_entries (job_id);

-- custody --------------------------------------------------------------
CREATE TABLE artifacts (
    artifact_id      text PRIMARY KEY,
    job_id           text NOT NULL REFERENCES jobs,
    chat_context_id  text NOT NULL,              -- denormalised, see note
    role             text NOT NULL,              -- PRIMARY POSTER TRANSCRIPT CAPTIONS ASSET SOURCE LOG
    audience         text NOT NULL DEFAULT 'LEARNER',  -- LEARNER | OPERATOR
    mime             text NOT NULL,
    rel_path         text,                       -- position in a bundle tree; NULL when standalone
    size_bytes       bigint NOT NULL,
    content_hash     text NOT NULL,
    storage_uri      text NOT NULL,
    probe            jsonb NOT NULL DEFAULT '{}',
    scan_verdict     text NOT NULL,              -- CLEAN | QUARANTINED
    validator_version text NOT NULL,
    published_at     timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX artifacts_by_context ON artifacts (chat_context_id, created_at DESC)
    WHERE scan_verdict = 'CLEAN' AND audience = 'LEARNER' AND role = 'PRIMARY';
CREATE INDEX artifacts_by_job     ON artifacts (job_id);
CREATE INDEX artifacts_by_hash    ON artifacts (content_hash);
```

`role`, `audience`, and `rel_path` replace the original single `kind` column (D078, D079). The
old enum folded three questions into one: what the file is for, what format it is, and who may
see it. `mime` already answered the second. The third had nowhere to live before: a harvested
agent log can pass every safety check, be `CLEAN`, and still be something no learner may fetch,
and `scan_verdict` is not the field to say so. The listing index carries both predicates so the chat
panel query cannot reach a row it should not show.

`chat_context_id` is denormalised onto `artifacts` so the chat panel's listing is one index
scan with no join, and so the partial index can exclude quarantined rows entirely. The cost is
one column that must be copied correctly at insert, in the one module that inserts artifacts.

Bytes are not in the database. `storage_uri` points at the object store. `content_hash` is the
addressing scheme, so the storage backend can change without touching a row.

## Frozen: API contract

MVP surface, in build order:

| Method | Path | Priority |
|--------|------|----------|
| GET | `/health` | P0 |
| POST | `/v1/jobs` | P0 |
| GET | `/v1/jobs/{job_id}` | P0 |
| GET | `/v1/contexts/{chat_context_id}/artifacts` | P0 |
| GET | `/v1/artifacts/{artifact_id}/content` | P0 |
| GET | `/v1/jobs` | P1 |
| GET | `/v1/jobs/{job_id}/events` | P1 |
| GET | `/v1/jobs/{job_id}/deliverable` | P1 |
| GET | `/v1/jobs/{job_id}/content` | P1 |
| GET | `/v1/me` | P2 |
| POST | `/v1/jobs/{job_id}/cancel` | P2 |
| GET | `/internal/metrics` | P2 |

Shapes are in `04-api-design.md`, and the Pydantic types are in `14-api-schema.md`. Three
conventions the MVP freezes:

```jsonc
// every list response
{"items": [...], "next_cursor": "opaque-or-null"}

// every error
{"error": {"code": "REQUEST_REJECTED", "message": "...", "details": {}}}

// every timestamp
"2026-08-05T09:12:03Z"
```

## Build stages

Each stage ends in something demonstrable. Do not start a stage before the one above is
demonstrable, because every stage after the first is easier to debug when the one below it is
known good.

### Stage 0. Contracts. P0

- [ ] `domain/` types: ids, `VideoJob`, `LessonBrief`, `Artifact`, enums
- [ ] `domain/errors.py`: `ErrorCode` + `ERROR_CATALOG`
- [ ] Pydantic request and response schemas for the five P0 endpoints, per `14-api-schema.md`
- [ ] `OutputContract` registry with `video.short.v1`, and its `OUTPUT_CONTRACT.json` rendering
- [ ] The migration above, applied by `create_all` or Alembic (Q-AA)
- [ ] Id derivation: `uuid5` helpers with fixed namespaces, unit tested

Done when: `uv run pytest tests/unit` passes with no application code, only types and rules.

### Stage 1. It runs. P0

- [ ] `deploy/Dockerfile`, one image
- [ ] `deploy/docker-compose.yml`: db, storage, api, worker
- [ ] `GET /health` returns `200` and reports database reachability
- [ ] SQL repositories for `jobs` and `idempotency_keys`, plus their contract test suite

Done when: `docker compose up` and `curl /health` succeeds from a clean checkout.

### Stage 2. Submit and read. P0

- [ ] `access` stub: fixed principal, `AccessScope`, claim-on-first-use context grant
- [ ] `POST /v1/jobs`: validate, derive `job_id`, insert job and work item in one transaction
- [ ] Idempotent replay: same key and body returns the same job; different body is `409`
- [ ] `GET /v1/jobs/{job_id}`, scoped, `404` for someone else's job

Done when: submitting twice with one key yields one row, and a second principal cannot read it.
The job sits at `QUEUED` forever, which is correct at this stage.

### Stage 3. The loop turns. P0

- [ ] Work item claim with `FOR UPDATE SKIP LOCKED`, lease, heartbeat
- [ ] Workflow engine: step protocol, checkpoint write, resume from checkpoint
- [ ] Steps: `intake`, `generate`, `harvest`, `verify`, `publish`, `teardown`
- [ ] `intake`: sanitiser, `SanitisedText`, brief sealing, template render to files
- [ ] `generation`: ports plus `ScriptedBackend` returning a committed fixture video
- [ ] `custody`: harvest, path allowlist, size cap, `ResultValidator` chain, object store write
- [ ] Job reaches `SUCCEEDED` with an `artifact_id`

Done when: a submitted job finishes on its own and the artifact row exists.

### Stage 4. Output retrieval. P0

- [ ] `GET /v1/contexts/{id}/artifacts`, scoped, cursor paged, quarantined rows excluded
- [ ] `GET /v1/artifacts/{id}/content`, scoped, streamed from the object store, `ETag`
- [ ] `GET /v1/jobs/{id}` carries the artifact link once succeeded

Done when: the five-step demo script at the top of this page runs end to end.

**MVP is complete here.** Everything below raises quality or proves a claim the plan makes.

### Stage 5. It survives. P1

- [ ] Sweeper: expired run leases returned to the queue
- [ ] Restart the worker mid-job, watch it resume from its checkpoint
- [ ] Retry policy with backoff and jitter, failure classification
- [ ] Compensations, including `teardown` on every exit path
- [ ] Tests: crash injection between steps, duplicate delivery to two runners

Done when: `docker compose restart worker` during a job still ends in `SUCCEEDED`, without
repeating a completed step.

### Stage 6. It is observable. P1

- [ ] `job_events` written through the outbox in the same transaction as state changes
- [ ] `GET /v1/jobs/{job_id}/events` with a cursor, user-visible events only
- [ ] Monotonic `progress_percent` derived from the step cursor
- [ ] Test: `detail` never appears in any client response

Done when: a reviewer can watch a job's history rather than only its current state.

### Stage 7. It resists. P1

- [ ] Guard rule set behind `GuardPort`
- [ ] Intent classifier and concept registry, deny by default
- [ ] Fence escaping in the brief template, with its own test
- [ ] `tests/redteam/` corpus, each fixture asserting the layer that stops it

Done when: the corpus passes and each stop is attributed to a named layer.

### Stage 8. It costs something. P1

- [ ] `CostEntry` written at every step boundary
- [ ] Precondition check against the remaining budget before each step
- [ ] Three ceilings: job, chat context, principal per day
- [ ] `MEASURED` and `CLAIMED` kept separate in the job view

Done when: a job with a low ceiling stops with `BUDGET_EXHAUSTED` and compensates cleanly.

### Stage 9. It generates. P2

- [ ] `LocalProcessBackend`
- [ ] Template repo with agent skills and check scripts
- [ ] Degradation ladder rungs 2 and 3
- [ ] Real media probe via ffprobe

Done when: the fixture is replaced by a video the system actually made.

## Priority summary

| Priority | Stages | What it buys |
|----------|--------|--------------|
| P0 | 0 to 4 | The MVP definition of done. A reviewer can use it |
| P1 | 5 to 8 | The claims this plan makes about failure, observation, safety, and cost |
| P2 | 9 | A real video instead of a fixture |

The honest ordering argument: P1 is where this design earns its keep, and P0 alone is a job
queue anyone could write. But P0 has to exist first, because none of the P1 claims can be
demonstrated without something to demonstrate them on.

## Explicitly not in the MVP

Real authentication, admin API, rate limiting, cancellation, webhooks or server-sent events,
multi-subject packs, artifact lifecycle and garbage collection, OpenTelemetry, sandbox or
cloud placement, and dead-letter handling. Each has a line in `10-scope-matrix.md` or
`../open-questions.md`.
