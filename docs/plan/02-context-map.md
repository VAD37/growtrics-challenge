# Context map and data flow

The point of this doc is the seams. For each one: who calls whom, what payload crosses, and
what the receiving side is allowed to assume about it.

## Map

```
        ┌───────────────────────────────────────────────────────────────┐
        │  Client (frontend, curl)                                      │
        └───────────────┬───────────────────────────────┬───────────────┘
                        │ CreateJobRequest              │ GET job / events / artifact
                        ▼                               ▼
        ┌───────────────────────────────────────────────────────────────┐
        │  API context   validation, serialisation, no domain rules     │
        └───────┬──────────────────┬────────────────────────────┬───────┘
                │ SubmitJob cmd    │ JobQuery                   │ ArtifactQuery
                ▼                  ▼                            ▼
     ┌───────────────────┐  ┌──────────────────────┐  ┌────────────────────┐
     │ Intake & Safety   │  │ Job Orchestration    │  │ Artifact Custody   │
     │ sanitise, guard,  │─▶│ CORE                 │─▶│ harvest, verify,   │
     │ seal LessonBrief  │  │ job + run aggregates │  │ store, serve       │
     └───────────────────┘  │ workflow engine      │  └─────────┬──────────┘
                 ▲          └───┬──────────────┬───┘            │ pull
                 │              │              │                │
        ┌────────┴──────┐  ┌────▼────────┐  ┌──▼──────────────────────────┐
        │ Access        │  │ Observ.     │  │ Generation ACL              │
        │ authn, balance│  │ events,     │  │ placement + session control │
        │ STUB          │  │ metrics STUB│  └──────────┬──────────────────┘
        └───────────────┘  └─────────────┘             │ RenderRequest (files)
                                                       ▼
                              ┌──────────────────────────────────────────┐
                              │  Generation (external, untrusted)        │
                              │  worker machine + template repo + agent  │
                              └──────────────────────────────────────────┘
```

Two properties of this picture matter more than the boxes:

1. **The Generation context is downstream of everything and upstream of nothing.** It
   receives files and returns files. It never calls back into the backend, never writes to
   our storage, and never appears on a path the client touches.
2. **Artifact Custody sits between Generation and the client.** Bytes only reach a learner
   after passing through custody. This is what makes "artifacts are served directly to the
   user with no connection to the cloud agent" true structurally instead of by convention.

## Relationships

| Upstream | Downstream | Pattern | Note |
|----------|------------|---------|------|
| API | Orchestration | Customer/supplier | API is a thin adapter. Domain owns the contract. |
| Orchestration | Intake | Customer/supplier | Orchestration asks for a Brief and gets a verdict with it. |
| Orchestration | Access | Conformist, stubbed | We take whatever the account service says. No local balance logic. |
| Orchestration | Custody | Customer/supplier | Custody exposes publish and quarantine; orchestration does not touch storage. |
| Generation | Backend | **Anti-corruption layer** | Nothing from a worker enters the domain untranslated. |
| Everything | Observability | Published language | `JobEvent` is the shared vocabulary. Emitters do not know the sinks. |

The anti-corruption layer is the one that earns its cost. A worker returns paths, exit codes,
agent logs, and a self-declared manifest. None of those are domain types. The ACL maps them
to `ArtifactDescriptor`, `RenderProgress`, and `RunOutcome`, drops everything unrecognised,
and treats every field as hostile until checked.

## Hop by hop

### 1. Client to API

```
POST /v1/jobs
Idempotency-Key: 4f3a...
{
  "instruction": "why do atoms form covalent bonds?",
  "context": [
    {"kind": "LEVEL", "text": "grade 9"},
    {"kind": "PRIOR_TOPIC", "text": "ionic bonding"}
  ],
  "options": {"max_duration_s": 90, "language": "en"}
}
```

API checks shape, sizes, and enum membership only. It does not decide whether the request is
acceptable; that is Intake's job and it needs domain context to answer.

### 2. API to Orchestration

```
SubmitJob(
  principal: Principal,          # resolved by the auth stub
  raw: RawLessonRequest,         # instruction + context items, still untrusted
  idempotency_key: str | None,
  requested_contract: ContractVersion,
)  ->  JobAccepted(job_id, status, links) | JobRejected(code, reason)
```

A command object, not loose kwargs, so the same call is replayable from a test or a CLI
without a HTTP layer.

### 3. Orchestration to Intake

```
SealBrief(raw: RawLessonRequest, subject_hint: SubjectId | None)
  -> BriefSealed(brief: LessonBrief, verdict: GuardVerdict)
  |  BriefRefused(verdict: GuardVerdict)
```

The verdict travels with the brief rather than being thrown away on success. Downstream steps
and the event feed both want to know that a request scored 0.3 on injection heuristics even
though it passed.

### 4. Orchestration to Generation ACL

The only user-derived data crossing this seam is a set of files. No prompt strings, no role
messages, no JSON blob that an agent might read as instructions.

```
RenderRequest(
  session_id:     RenderSessionId,      # ours, idempotency key for the worker side
  trace_id:       TraceId,
  template:       TemplateRef,          # repo url + pinned commit
  workspace:      BriefBundle,          # the files listed below
  limits:         ExecutionLimits,      # wall clock, model calls, tokens, disk, egress rules
  output_contract: OutputContract,      # what must exist in out/ when done
)
```

`BriefBundle` is a plain directory image:

```
workspace/
  BRIEF.md            rendered from LessonBrief; user text inside a fenced data block
  CONTEXT.md          one labelled section per ContextItem kind
  CONSTRAINTS.md      duration, language, reading level, style
  OUTPUT_CONTRACT.json  required paths, kinds, mime types, check names
  .trace              trace_id and session_id, for log correlation
```

The agent's own instructions are not in this bundle. They live in the template repo as
committed skills, which we version and review. See `06-trust-boundary.md`.

### 5. Generation ACL back to Orchestration

Pull only. The worker never initiates.

```
RenderProgress(session_id, phase, note, iterations_done, at)
RenderOutcome(session_id, status: QUALIFIED|EXHAUSTED|FAILED, declared: list[ArtifactDescriptor], usage: Usage)
```

`declared` is the worker's claim. It is a hint used to decide what to fetch, never evidence
that the file is what it says it is.

### 6. Custody ingest

```
ArtifactCandidate(session_id, descriptor, stream)
  -> ArtifactVerified(artifact: Artifact)
  |  ArtifactQuarantined(reason: QuarantineReason, candidate_ref)
```

Verification is independent of the worker's checks. Same tests, run again, on our side, on
the bytes we actually received.

### 7. Custody to client

```
GET /v1/artifacts/{artifact_id}/content
  -> 200 with bytes from our storage, or 302 to a signed URL of our storage
```

There is no code path where this handler can reach a worker host. That is enforced by the
module layout in `03-module-layout.md`: the API package may import custody's read port and
nothing from `generation/`.

## Data classification

Every payload above falls into one of four bands. The band decides what may touch it.

| Band | Examples | Rule |
|------|----------|------|
| Untrusted | `RawLessonRequest`, agent logs, declared manifest, harvested bytes | Never rendered into an instruction position. Never logged verbatim. Never trusted for control flow without validation. |
| Sealed | `LessonBrief`, `BriefBundle` | Derived from untrusted input, but sanitised, typed, hashed, and immutable. Safe to persist and to hand to a worker. |
| Domain | `VideoJob`, `WorkflowRun`, `Artifact`, `JobEvent` | Ours. Trusted inside the process boundary. |
| Secret | provider keys, signing keys, storage credentials | Never enters a workspace, never appears in an event, never crosses the generation seam. |

`@audit` the sharpest rule in this table is that untrusted data may not reach an instruction
position. Every new field added to `BriefBundle` needs a check that it cannot escape its
fenced block. A test corpus for this lives in `06-trust-boundary.md`.
