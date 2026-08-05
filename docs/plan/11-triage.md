# Triage: review round 2b against the plan

Input is the second half of `../notes.md`. Each insight gets one of three verdicts.

- **Confirms** the plan already says this. No edit, sometimes a sharpening.
- **Changes** the plan said something else. The plan is wrong and moves.
- **Adds** the plan was silent. New material.

## Verdicts

| # | Insight | Verdict | Where |
|---|---------|---------|-------|
| 1 | Chat window, artifact panel per chat context, artifact history, job status window | **Adds** | 01, 04 |
| 2 | Frontend sends user id, chat context, instruction, personalised context | **Adds** (chat context), confirms the rest | 04 |
| 3 | Separate the user prompt from the job prompt | Confirms | 01, 06 |
| 4 | Backend builds its own job prompt, a plan, from query plus context; templated for quality control | Confirms, **sharpens** into a versioned template registry | 01, 06 |
| 5 | SQL as the only source of truth | **Changes**, supersedes D006 | 03, 07, 08 |
| 6 | A long-term storage system | Confirms, pins the split | 03, 07 |
| 7 | Docker compose | **Adds** | 03, 10 |
| 8 | Metric events in the database, nothing fancy | **Changes** the sink, confirms the vocabulary | 08 |
| 9 | Observation reads the database as source of truth | **Changes**, kills the in-memory projection | 08 |
| 10 | Token cost computed inside each step, to prevent abuse | **Changes**, cost was deferred | 04, 05 |
| 11 | Separation of data from logic, system built around the data schema | Confirms | 03 |
| 12 | A clear errors file, not bare strings | Confirms, **sharpens** into a catalog | 03, 04 |
| 13 | Validation on every workflow step input | Confirms | 05 |
| 14 | Strict schema format for JSON and jobs | Confirms | 01, 04 |
| 15 | Every job derives from an idempotency key, the main key for the system | **Changes**, the plan had it optional and local | 04, 05, 07 |
| 16 | Agent system is its own repo; backend spawns and passes information, does not care what it does, but validates results through an upgradeable interface | Confirms | 09 |
| 17 | Core is modules; each pipeline part is atomic, sharing only types and data | Confirms, **sharpens** into an enforced import rule | 03, 05 |

Five rows move the design. The rest tighten it.

---

## 1. Chat context is a first-class dimension

The plan scoped everything to a principal. The frontend is a chat product, so the unit a
learner recognises is a conversation, not an account.

`ChatContextId` lands on `VideoJob` and is carried on every artifact through its job. Job
listing and artifact listing both filter by it.

The boundary call: **we index by chat context, we do not own it.** No conversation aggregate,
no messages, no ordering rules, no lifecycle. It is a foreign reference with an index on it.
Modelling the conversation would pull chat product concerns into a video service.

`@audit` an unowned identifier arriving from a client is an authorisation question. A chat
context must be checked against the principal, or one user can list another's artifacts by
guessing an id. The access stub gains one method: `assert_can_read_context(principal,
chat_context_id)`.

## 4. The job prompt becomes a versioned template

The plan already had the intermediary: `LessonBrief`, sealed and hashed. The insight adds
*why the shape matters*, which is quality control by templating.

So the renderer stops being one function and becomes a registry:

```
intake/templates/
  v1/
    BRIEF.md.tmpl
    CONTEXT.md.tmpl
    CONSTRAINTS.md.tmpl
    OUTPUT_CONTRACT.v1.json
  v2/...
```

`template_version` joins the brief hash. Two consequences worth having: a prompt-quality
change is a reviewable diff rather than an edit to a Python string literal, and a regression
can be bisected by re-rendering an old brief under a new template and comparing outputs.

Vocabulary: the notes call it the job prompt or the plan. The domain type stays `LessonBrief`
because "prompt" is reserved for what the user wrote, but `plan/01-domain-model.md` now says
the two names refer to the same thing.

## 5, 6, 8, 9. SQL is the source of truth

This supersedes D006. In-memory repositories drop to test doubles.

One database, one truth, and everything observable is a table:

| Table | Holds |
|-------|-------|
| `idempotency_keys` | key, principal, body digest, resolved job id |
| `jobs` | the `VideoJob` aggregate, version column for compare-and-set |
| `briefs` | sealed brief, hash, template version, guard verdict |
| `workflow_runs` | run, workflow version, lease owner, lease expiry |
| `step_records` | checkpoints, keyed `(run_id, step_name)`, with `input_digest` |
| `job_events` | the event stream, dense `seq` per job, `visibility` column |
| `cost_entries` | per-step token and money ledger |
| `artifacts` | metadata, content hash, storage uri, scan verdict |
| `work_items` | the queue |
| `outbox` | state change plus event, published by a relay |

Three things get cheaper the moment a database exists, and all three were `@TODO` before:

**The queue stops being an `asyncio.Queue`.** `SELECT ... FOR UPDATE SKIP LOCKED` over
`work_items` gives at-least-once delivery, visibility timeouts, and lease reclaim without a
broker. The queue and the state it guards commit in the same transaction, which is the whole
reason the outbox pattern existed in the plan.

**The outbox becomes real rather than deferred.** State change and event row commit together.
`plan/07-distribution.md` had this as an `@TODO` justified by single-process atomicity; with
SQL it is a table and twenty lines of relay.

**Observability stops being a second system.** `job_events` is read by the projection, the
user feed, and the metric rollups. `MetricsPort` keeps its vocabulary and its adapter becomes
an insert. No console registry, no OpenTelemetry this round.

Bytes stay out of the database. `artifacts` holds metadata and a pointer; the long-term store
holds the file. "Source of truth" is about state, not about blobs, and putting a 5 MB mp4 in a
row makes every backup and every query worse.

`@audit` metric rollups computed by querying `job_events` will not scale past a demo. The fix
is a rollup table written by the relay, not a fancier sink. Marked, not built.

## 7. Docker compose

New deliverable, and the thing that makes the rest checkable by a reviewer in one command.

```
services:
  db        postgres, volume
  storage   minio, volume            # the long-term store
  api       the FastAPI app
  worker    the same image, runner + sweeper entrypoint
```

`api` and `worker` are one image with two entrypoints. That is the honest expression of
`plan/07-distribution.md`: same code, different role, separately scalable.

`@TODO` a migration step. Alembic, or a startup `create_all` for the demo with a note that it
is not a migration strategy.

## 10. Cost accounting moves inside the workflow

The plan deferred cost entirely. The insight reframes it: this is not billing, it is abuse
prevention, and it belongs at every step boundary rather than in a report at the end.

```
CostEntry(job_id, run_id, step_name, attempt, model, input_tokens, output_tokens,
          unit_price_version, amount_micros, source: MEASURED | CLAIMED, at)
```

Enforced in three places:

1. **Before a step runs.** The step declares an estimate. If remaining budget cannot cover it,
   the run stops with `BUDGET_EXHAUSTED` rather than starting work it cannot finish.
2. **After a step returns.** Actual usage is debited. A step that overshot its estimate by a
   wide margin is a metric worth watching.
3. **At three ceilings.** Per job, per chat context, per principal per day. The per-context
   ceiling is the one that stops a single conversation from looping expensive regenerations.

The honest part: **worker-reported usage is a claim.** The agent runs on its own credentials
in its own repo, so `usage` in the manifest is unverified input like everything else that
crosses that seam. It is recorded with `source: CLAIMED` and never mixed with `MEASURED`
rows. Any ceiling enforced purely on claimed numbers is enforceable only by the session
timeout and the iteration limit, which is why those exist. See Q-R.

`@audit` a ceiling that only the untrusted side can measure is not a ceiling. The real control
on agent spend is the wall clock and the iteration cap the backend imposes, not the token
count the agent reports.

## 12. Errors become a catalog

`domain/errors.py` holds one enum and one table. No error message is written at a raise site.

```python
class ErrorCode(StrEnum):
    REQUEST_REJECTED = "REQUEST_REJECTED"
    ...

ERROR_CATALOG: dict[ErrorCode, ErrorSpec] = {
    ErrorCode.REQUEST_REJECTED: ErrorSpec(
        http=422,
        retryable=False,
        user_message="We could not process that request.",
        operator_hint="intake guard refused; see guard_verdict in detail",
    ),
    ...
}
```

Everything downstream reads from it: the HTTP status mapping, the retry classification, the
learner-facing sentence, and the operator hint. One place to audit for leaks, one place to
translate later.

`@TODO` a test that walks every `raise` of the domain error type and asserts the code is in
the catalog.

## 15. The idempotency spine

The plan had an optional `Idempotency-Key` header and separate per-step keys. The insight
makes it the backbone, which is stronger and simpler.

`Idempotency-Key` becomes **required** on `POST /v1/jobs`. The job id is derived, not
generated:

```
job_id        = uuid5(NS_JOB, principal_id | chat_context_id | idempotency_key)
run_id        = uuid5(NS_RUN, job_id | run_ordinal)
session_id    = uuid5(NS_SESSION, run_id | attempt_group)
step_effect   = uuid5(NS_STEP, run_id | step_name | attempt_group)
artifact_id   = uuid5(NS_ART, job_id | content_hash)
```

One root key propagates to every derived identity in the system. Consequences:

- A resubmission computes the same `job_id` and collides on the primary key. Deduplication
  becomes a constraint violation rather than a read-then-write race.
- Every external effect is addressable before it is performed, so at-least-once delivery is
  safe by construction instead of by discipline in each step.
- A worker session is reachable by an id both sides can compute, so a retry after a backend
  crash can find and reuse the session it already opened rather than starting a second one.

The `idempotency_keys` table still exists, storing the body digest, so a reused key with a
different body is a `409` rather than a silent alias to someone else's job.

## Confirmed, with the sharpening noted

**3 and 16.** Already the design. The one addition from insight 16 is that the agent
repository is explicitly not ours to reason about, which makes the validation interface the
only contract that matters. `plan/09-generation-worker.md` gains a named
`ResultValidator` port with a version, so upgrading validation is a swap rather than an edit
spread across custody.

**11, 13, 14, 17.** The plan's typed step contract, pure `domain/` package, and per-context
packages already say this. What was missing was enforcement, so it becomes an import-linter
contract in CI:

```
orchestration.steps.<X> may not import orchestration.steps.<Y>
any step may import domain.*
domain may import nothing from app.*
api may not import storage.*, generation.*, or any adapter
```

An architectural rule that is not checked is a comment.

## Scope movement

| Row in `10-scope-matrix.md` | Was | Now | Cause |
|-----|-----|-----|-------|
| 20 in-memory repositories | BUILD | test doubles only | insight 5 |
| 21 SQL repositories | DEFER | **BUILD** | insight 5 |
| 22 transactional outbox | DEFER | **BUILD** | insight 5, nearly free with SQL |
| 17 metrics port, console sink | STUB | **BUILD**, DB sink | insights 8, 9 |
| 18 OpenTelemetry | DEFER | DEFER | unchanged |
| 40 cost model | DEFER | **BUILD**, as step-level metering | insight 10 |
| new: docker compose | absent | **BUILD** | insight 7 |
| new: error catalog | implied by row 1 | **BUILD**, own row | insight 12 |
| new: template registry | implied by row 11 | **BUILD**, own row | insight 4 |
| new: chat context indexing and authorisation | absent | **BUILD** | insights 1, 2 |
| new: import-linter contract | `@TODO` | **BUILD** | insight 17 |
| 13 idempotency handling | BUILD | BUILD, scope widened to derived ids | insight 15 |

Net effect on effort: the demo grew. SQL, compose, and cost metering are real work that the
in-memory plan avoided. The trade is that the result is a system a reviewer can run, restart,
and inspect, rather than one that forgets everything when the process exits.
