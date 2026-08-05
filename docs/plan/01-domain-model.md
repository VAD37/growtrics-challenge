# Domain model

## Ubiquitous language

Words used the same way in docs, code, API fields, log lines, and test names. If a word is
not here, it should not appear in a type name.

| Term | Meaning |
|------|---------|
| Principal | Whoever submits work. Carries entitlement and a token balance. Stubbed. |
| Chat Context | The conversation a request came from. A foreign id we index by and never own. |
| Lesson Request | The raw thing a Principal sent: instruction plus context items. Untrusted. |
| Lesson Brief | The sanitised, wrapped, sealed instruction package the backend produces from a Lesson Request. Immutable. The only form of user intent that travels downstream. The notes call this the job prompt, or the plan; same object. |
| Brief Template | The versioned set of files the brief is rendered through. Where input quality is controlled. |
| Cost Entry | One step's token and money usage, marked as measured by us or claimed by a worker. |
| Output Contract | The declaration of what a finished job must produce. Versioned, ours, never the agent's. |
| Video Job | The unit of work a Principal can name, query, and observe. |
| Workflow Run | One execution of the job's workflow. A job can have more than one run over its life. |
| Step | One named unit inside a run, with typed input and output, a retry policy, and a compensation. |
| Checkpoint | The persisted result of a completed step, keyed so a resumed run does not repeat it. |
| Worker Lease | A time-bounded claim on a machine that can host generation. |
| Render Session | One generation attempt on a leased worker. Lives outside our trust boundary. |
| Artifact Candidate | A file harvested from a Render Session that has not yet been verified. |
| Artifact | A verified, stored, servable file with a content hash and a manifest entry. |
| Job Event | An appended fact about a job. The single source for status, progress, and metrics. |
| Budget Hold | A reserved amount of a Principal's balance, released or settled at the end. Stubbed. |

Words deliberately avoided: "task" (overloaded with worker-queue tasks), "pipeline" as a
noun for the domain object (a run is a Workflow Run; the pipeline is the code that executes
it), "prompt" for anything a Principal wrote (that is a Lesson Request or a Brief).

## Subdomains

| Subdomain | Type | Why |
|-----------|------|-----|
| Job Orchestration | Core | The graded skill is the lifecycle, not the video. Owns the workflow engine. |
| Intake and Safety | Core | Turning untrusted text into a sealed Brief is where the system earns trust (N4). |
| Artifact Custody | Supporting | Harvest, verify, store, serve. Distinct lifetime and threat model from job state. |
| Generation | Supporting, external | Where the video is made. Replaceable. Behind an anti-corruption layer. |
| Access | Generic | Identity, entitlement, balance. Stub. |
| Observability | Generic | Events and metrics. Stub sinks. |

Effort follows this table. Core gets real code and real tests. Generic gets an interface and
a fake.

## Aggregates

An aggregate is a consistency boundary. One transaction touches one aggregate. Everything
else is reached by id and reconciled through events.

### VideoJob (root)

```
VideoJob
  job_id              JobId           # DERIVED, see the idempotency spine below
  principal_id        PrincipalId
  chat_context_id     ChatContextId   # foreign, indexed, not owned
  idempotency_key     str             # required
  request_digest      Sha256          # of the raw Lesson Request
  brief_ref           BriefRef | None # id + hash, set once
  status              JobStatus       # QUEUED RUNNING SUCCEEDED FAILED CANCELLED
  stage               StageName       # coarse label for humans, mirrors the current step
  attempt             int
  output_contract     ContractVersion
  artifact_ref        ArtifactRef | None
  budget              JobBudget       # ceiling, spent, source split
  failure             JobFailure | None
  version             int             # optimistic concurrency
  created_at, updated_at
```

Invariants:

1. `status == SUCCEEDED` implies `artifact_ref` is set and points at a verified Artifact.
   Verification happens inside the transition, never after it (carried over from D011).
2. `status == FAILED` implies `failure.code` and `failure.stage` are set, and the message is
   safe to show a learner (N5).
3. `brief_ref` is write-once. A new Brief means a new job.
4. Terminal statuses are terminal. No transition leaves `SUCCEEDED`, `FAILED`, or
   `CANCELLED`.
5. A job holds at most one active Workflow Run.
6. `budget.spent` never exceeds `budget.ceiling`. The check is a step precondition, not a
   report written afterwards.

### The idempotency spine

Ids are derived from one root key rather than generated, so every identity in the system is
computable by both sides before the thing exists.

```
job_id      = uuid5(NS_JOB,     principal_id | chat_context_id | idempotency_key)
run_id      = uuid5(NS_RUN,     job_id | run_ordinal)
session_id  = uuid5(NS_SESSION, run_id | attempt_group)
step_effect = uuid5(NS_STEP,    run_id | step_name | attempt_group)
artifact_id = uuid5(NS_ART,     job_id | content_hash)
```

A resubmission collides on the primary key instead of racing a read-then-write. A step retried
after a crash addresses the same external effect it addressed before. This is what makes
at-least-once delivery safe by construction rather than by discipline in each step.

### Chat context

Indexed, not owned. No conversation aggregate, no messages, no ordering, no lifecycle. Jobs
and artifacts list by it because the frontend is a chat product and that is the unit a learner
recognises.

`@audit` an id supplied by a client is an authorisation question, not a filter. Reads scoped
by `chat_context_id` must first check the context belongs to the principal, or one user
enumerates another's artifacts by guessing.

### WorkflowRun (root)

```
WorkflowRun
  run_id              RunId
  job_id              JobId
  workflow_version    str            # pinned at creation so resume is safe
  steps               list[StepRecord]
  cursor              StepName
  lease_owner         WorkerId | None
  lease_expires_at    datetime | None
  started_at, ended_at
  outcome             RunOutcome | None
```

Split from `VideoJob` because it is written far more often (every step, every heartbeat) and
read by different code. The job is the public face; the run is the machinery.

`@audit` two aggregates means job status is eventually consistent with run progress. In the
demo both live in one process behind one store, so the window is nil. If the run store ever
moves out of process, the reconciliation path needs its own test.

### LessonBrief (root, immutable)

```
LessonBrief
  brief_id         BriefId
  brief_hash       Sha256          # over the canonical serialisation
  template_version TemplateVersion # which template rendered the files
  subject          SubjectId
  concept_id       ConceptId | None
  instruction      SanitisedText
  context_items    list[ContextItem]   # closed set of kinds
  constraints      BriefConstraints    # duration, language, reading level
  guard_verdict    GuardVerdict        # decision, matched rules, risk score
  sealed_at        datetime
```

This is the job prompt. The user wrote an instruction; the backend wrote this. The separation
is the point: the thing that reaches a worker is authored by us, from a versioned template,
with the user's words as one labelled input among several.

`template_version` is part of the hash. A prompt-quality change becomes a reviewable diff
instead of an edit to a string literal, and a regression can be bisected by re-rendering an
old brief under a new template.

Sealed means no code may mutate it. Anything that wants a different Brief creates a new one.
The hash feeds the content key and the audit trail, so the exact bytes that reached a worker
can be reproduced from the record.

### Artifact (root)

```
Artifact
  artifact_id      ArtifactId
  job_id           JobId
  content_hash     Sha256
  kind             ArtifactKind    # VIDEO POSTER TRANSCRIPT SOURCE LOG
  mime             str
  size_bytes       int
  storage_uri      str             # ours, never the worker's
  probe            MediaProbe      # duration, streams, resolution
  scan             ScanVerdict     # CLEAN QUARANTINED
  published_at     datetime | None
```

Only `CLEAN` artifacts get an id a client can reach. A quarantined candidate keeps a record
for operators and no public route.

### CostEntry (append-only, keyed by job)

```
CostEntry
  entry_id           EntryId
  job_id             JobId
  run_id             RunId
  step_name          StepName
  attempt            int
  model              str | None
  input_tokens       int
  output_tokens      int
  unit_price_version str
  amount_micros      int
  source             MEASURED | CLAIMED
  at                 datetime
```

Written at every step boundary, never edited. `JobBudget.spent` is the sum, maintained on the
job so a precondition check is one read.

`source` is the field that keeps this honest. Usage the backend metered itself is `MEASURED`.
Usage a worker reported in its manifest is `CLAIMED`, because the agent runs on its own
credentials in its own repository and its numbers are untrusted input like everything else
crossing that seam.

`@audit` a ceiling enforced on `CLAIMED` numbers is not enforced. The real controls on agent
spend are the wall clock and the iteration cap the backend imposes, not the token count the
agent reports back.

### Account (root, stubbed)

```
Account
  principal_id     PrincipalId
  balance_tokens   int
  holds            list[BudgetHold]
```

Real accounting is out of scope this round. The interface exists so the workflow's admit and
settle steps have something to call, and so a later swap does not reshape the workflow.

## Value objects

`JobId`, `RunId`, `BriefId`, `ArtifactId`, `PrincipalId`, `Sha256`, `ContractVersion`,
`StageName`, `StepName`, `SanitisedText`, `ContextItem`, `GuardVerdict`, `MediaProbe`,
`ScanVerdict`, `JobFailure`, `WorkerLease`, `RenderSessionRef`, `ArtifactDescriptor`.

Two of these carry more weight than the rest:

**`SanitisedText`** cannot be constructed from a `str` directly. It is produced only by the
intake sanitiser. Any function that accepts user-authored text takes `SanitisedText`, so
"did this text get sanitised" becomes a type error rather than a review question.

**`ContextItem`** has a closed `kind` enum (`MEMORY`, `LEVEL`, `PRIOR_TOPIC`, `MISCONCEPTION`,
`LANGUAGE`, `NOTE`). Free-form context is how prompt injection arrives dressed as data, so
the shape is fixed and each kind is rendered into its own labelled section of the brief
files.

## Domain events

Past tense, immutable, appended in order per job. This one stream feeds three readers: the
job projection, the metrics sink, and the user-visible progress feed. Building three separate
mechanisms for those three needs is the usual way they drift apart.

| Event | Emitted when | Visible to user |
|-------|--------------|-----------------|
| `JobSubmitted` | Request accepted and persisted | yes |
| `RequestRejected` | Guard refused before any spend | yes |
| `BriefSealed` | Sanitised brief written and hashed | yes |
| `BudgetHeld` / `BudgetReleased` / `BudgetSettled` | Admission and teardown | yes |
| `RunStarted` / `RunResumed` | Workflow run begins or picks up from a checkpoint | no |
| `StepStarted` / `StepSucceeded` / `StepFailed` | Every step boundary | mapped |
| `WorkerLeased` / `WorkerReleased` | Placement acquired or freed | no |
| `RenderSessionOpened` / `RenderProgressed` | Generation running, heartbeat carries a note | yes |
| `ArtifactsHarvested` | Candidates pulled from the worker | no |
| `ArtifactVerified` / `ArtifactQuarantined` | Independent check result | mapped |
| `ArtifactPublished` | Stored and reachable | yes |
| `JobSucceeded` / `JobFailed` / `JobCancelled` | Terminal transition | yes |

"mapped" means the operator event is real but the user sees a plainer version. A learner does
not need to read `StepFailed(step=harvest, err=ECONNRESET, attempt=2)`; they need
"collecting the video, retrying".

## Lifecycle

```
                      RequestRejected
                            ▲
                            │ guard denies
   POST /v1/jobs ──▶ [intake] ──▶ QUEUED ──▶ RUNNING ──┬──▶ SUCCEEDED
                                     ▲          │      │
                                     │          │      └──▶ FAILED
                          lease expired,        │
                          sweeper requeues      └──▶ CANCELLED (best effort)
```

`RUNNING` walks the step cursor. `stage` on the job mirrors the cursor so a client sees
movement without learning the step vocabulary. `attempt` counts retries of the current step,
not of the job, and resets when the cursor advances.

Guard rejection happens before `QUEUED` on purpose. A refused request costs one sanitiser
pass and no worker, no model call, and no budget hold.
