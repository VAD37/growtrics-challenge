# Module layout

One package per bounded context, plus a thin composition root. Contexts talk through ports,
never by importing each other's internals.

```
src/app/
├── main.py                  composition root: build adapters, wire ports, mount routers
├── config.py                settings, feature flags, limits
│
├── api/                     API context. FastAPI only lives here.
│   ├── routers/             jobs.py, artifacts.py, me.py, health.py, internal.py
│   ├── schemas/             request and response models (Pydantic v2)
│   ├── errors.py            domain failure -> HTTP envelope mapping
│   └── deps.py              auth stub, pagination, idempotency header handling
│
├── domain/                  no I/O, no framework, no async. Pure types and rules.
│   ├── ids.py               JobId, RunId, BriefId, ArtifactId, PrincipalId, Sha256
│   ├── job.py               VideoJob aggregate + transitions
│   ├── run.py               WorkflowRun, StepRecord, RunOutcome
│   ├── brief.py             LessonBrief, ContextItem, SanitisedText, GuardVerdict
│   ├── artifact.py          Artifact, ArtifactDescriptor, MediaProbe, ScanVerdict
│   ├── cost.py              CostEntry, JobBudget, unit price table
│   ├── events.py            JobEvent and the event union
│   └── errors.py            ErrorCode enum + ERROR_CATALOG. No message is written elsewhere
│
├── orchestration/           CORE. Owns the job and run aggregates.
│   ├── service.py           SubmitJob, QueryJob, CancelJob use cases
│   ├── engine/              the workflow engine (see 05)
│   │   ├── definition.py    Workflow, Step, RetryPolicy, Compensation
│   │   ├── runner.py        executes a run, writes checkpoints, emits events
│   │   ├── policy.py        backoff, budgets, failure classification
│   │   └── sweeper.py       lease expiry, orphan recovery
│   ├── steps/               one module per step; atomic, no step imports another
│   ├── budget.py            per-step cost precondition and debit
│   └── ports.py             JobRepository, RunRepository, Clock, Queue, UnitOfWork
│
├── intake/                  CORE. Untrusted text in, sealed brief out.
│   ├── sanitiser.py         normalise, cap, strip; the only producer of SanitisedText
│   ├── guard.py             GuardPort + rule-based default implementation
│   ├── classifier.py        is this a learning request, and about what
│   ├── sealer.py            build + hash the LessonBrief
│   ├── rendering.py         LessonBrief -> BriefBundle files, through a template
│   └── templates/v1/        BRIEF.md.tmpl, CONTEXT.md.tmpl, CONSTRAINTS.md.tmpl, contract
│
├── custody/                 Artifact harvest, verify, store, serve.
│   ├── harvester.py         pull candidates through the generation ACL
│   ├── verifier.py          structural probe, contract match, safety scan
│   ├── store.py             ArtifactStore port + local filesystem adapter
│   └── reader.py            the only read path the API is allowed to use
│
├── generation/              ACL over the external agent service. Deferred, interface first.
│   ├── ports.py             WorkerBroker, GenerationBackend, TemplateSource
│   ├── acl.py               worker payloads -> domain types, with validation
│   └── backends/            local.py (demo), scripted.py (fake), sandbox.py @TODO, cloud.py @TODO
│
├── access/                  STUB. Principal resolution, entitlement, balance holds.
│   └── stub.py
│
├── observability/           STUB sinks, real vocabulary.
│   ├── events.py            EventBus port, in-memory + log adapters
│   ├── metrics.py           MetricsPort, console + in-memory registry adapters
│   └── tracing.py           trace id creation and propagation
│
└── storage/                 Adapters for the repository ports.
    ├── sql/                 the real implementations. Postgres is the source of truth
    │   ├── models.py        tables listed in 11-triage.md
    │   ├── repositories.py  JobRepository, RunRepository, EventStore, CostLedger
    │   ├── queue.py         work_items with SELECT ... FOR UPDATE SKIP LOCKED
    │   └── outbox.py        state change + event in one transaction, plus the relay
    ├── memory/              test doubles only, kept honest by the contract suite
    └── objects/             the long-term artifact store (MinIO or S3, filesystem in tests)

tests/
├── unit/                    domain rules, step logic, guard rules
├── contract/                every port has one suite run against every adapter
├── redteam/                 the malicious-input corpus (06)
└── integration/             API to fake generation, end to end, against a real database

deploy/
├── docker-compose.yml       db, storage, api, worker
└── Dockerfile               one image, two entrypoints
```

## Dependency rule

```
api  ──▶ orchestration ──▶ domain
             │  │  │
             │  │  └────▶ intake  ──▶ domain
             │  └───────▶ custody ──▶ domain
             └──────────▶ generation (ports only) ──▶ domain

storage, observability, access  ──▶ domain      (adapters, imported only by main.py)
```

Rules an import-linter contract checks in CI. An architectural rule that is not checked is a
comment:

1. `domain/` imports nothing from `app/` except `domain/`. No FastAPI, no httpx, no asyncio
   primitives.
2. `api/` may not import `generation/`, `storage/`, or any adapter. It gets ports through
   `deps.py`.
3. `orchestration/` imports ports, never adapters. The concrete adapter is chosen in
   `main.py`.
4. `generation/` is imported by `orchestration/steps/` and `custody/harvester.py` and nowhere
   else.
5. No context imports another context's internal modules. `intake.sealer` is private to
   intake; orchestration calls `intake.service` only.
6. No step imports another step. `orchestration.steps.harvest` may not import
   `orchestration.steps.generate`. Steps share types from `domain/` and nothing else. This is
   what "each pipeline part is its own atomic module" means in enforceable terms.

The reason rule 2 exists in this shape: it makes "the client can never reach a worker" a
property of the import graph rather than a promise in a document.

## Ports, one line each

| Port | Owner | Demo adapter | Later |
|------|-------|--------------|-------|
| `JobRepository` | orchestration | SQL; in-memory double in unit tests | same |
| `RunRepository` | orchestration | SQL; in-memory double in unit tests | same |
| `EventStore` | observability | SQL `job_events` table | same, plus a rollup table |
| `CostLedger` | orchestration | SQL `cost_entries` table | same |
| `UnitOfWork` | orchestration | one database transaction | transaction per aggregate |
| `Queue` | orchestration | SQL `work_items`, `FOR UPDATE SKIP LOCKED` | broker if throughput demands it |
| `Clock` | orchestration | real clock; frozen clock in tests | same |
| `GuardPort` | intake | rule set plus fixture corpus | enterprise prompt guard `@TODO` |
| `ConceptRegistry` | intake | static registry for Q1 to Q3 | grown per subject |
| `WorkerBroker` | generation | local process broker | sandbox or cloud placement `@TODO` |
| `GenerationBackend` | generation | scripted fake returning a fixture video | agent worker |
| `TemplateSource` | generation | local path | pinned git commit |
| `ArtifactStore` | custody | MinIO in compose, filesystem in tests | S3 |
| `ResultValidator` | custody | versioned check chain over harvested bytes | upgraded independently |
| `MediaProbe` | custody | stub probe reading a sidecar | ffprobe `@TODO` |
| `SafetyScanner` | custody | allowlist and size checks | real scanner `@TODO` |
| `AccountService` | access | stub with a fixed balance | billing service |
| `MetricsPort` | observability | insert into a metrics table | OpenTelemetry `@TODO` |

Every port gets a contract test suite in `tests/contract/`. The suite is written once against
the port and run against each adapter, so the fake and the real implementation cannot drift
apart quietly. This is what makes heavy stubbing safe rather than decorative.
