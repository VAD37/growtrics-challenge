# Decision log

Append only. One line per decision. Never edit or delete a line; supersede it with a new
one and mark the old id in the new line. Format:

`YYYY-MM-DD | ID | decision | why`

---

2026-08-05 | D001 | Draft 1 is docs and architecture only, no application code | Locking the boundaries before writing code is the graded skill, and it makes the later code cheap to steer
2026-08-05 | D002 | Python 3.14, uv for env and deps, ruff for lint+format, pytest for tests | Latest stable Python; uv and ruff are the current fast default toolchain
2026-08-05 | D003 | Requirements extracted from the PDF into `docs/00-requirements.md` with stable IDs (R/Q/N/D) | Later docs cite requirement ids instead of restating the brief
2026-08-05 | D004 | Job state modelled as coarse `status` + fine `stage` + `attempt` rather than one flat enum | Client contract stays stable while the pipeline changes shape
2026-08-05 | D005 | Job store and artifact store are separate ports (`JobRepository`, `ArtifactStore`) | R8 asks for the boundary explicitly; state and bytes have different lifetimes
2026-08-05 | D006 | In-memory job store + local filesystem artifact store for draft 1 | Brief permits it; both sit behind protocols so the swap is mechanical
2026-08-05 | D007 | Every provider port ships a real implementation and a deterministic fake | Fakes make tests free and prove the boundary is real rather than decorative
2026-08-05 | D008 | LLM produces a schema-validated `LessonPlan`, never free prose | Constrained output is the main defence against flaky generation
2026-08-05 | D009 | The planner may only reference visual primitives the renderer implements | Turns model creativity into a cheap catchable validation error
2026-08-05 | D010 | Artifacts content-addressed by `content_key = hash(query, concept, pipeline_version, model_versions)` | Same input returns the identical artifact; repeat runs become consistent and free
2026-08-05 | D011 | Quality gate runs inside the transition to `SUCCEEDED`, not after | Guarantees a succeeded job always has a watchable artifact
2026-08-05 | D012 | Fallback ladder ends at static captioned slides + narration, which uses no model | Guarantees a floor; a learner never receives a blank video
2026-08-05 | D013 | Hand-checked reference `LessonPlan` committed for each of Q1–Q3 | Guarantees the three required queries always render, and doubles as test fixtures
2026-08-05 | D014 | Query → concept is a registry lookup, not an LLM call; unsupported queries 422 | Failing at the door beats a confident video about the wrong topic
2026-08-05 | D015 | Polling only in draft 1, with `Retry-After` on in-flight jobs; no webhooks or SSE | Smallest surface that models the waiting state clearly
2026-08-05 | D016 | Per-stage cost ledger exposed on the job payload | N1 asks what an artifact costs; make it an API answer, not a spreadsheet
2026-08-05 | D017 | Topic-specific assets (prompts, fact cards, primitives) live in a `subjects/` registry | Adding physics becomes a directory, not a refactor
2026-08-05 | D018 | Media tech stack (renderer, TTS, compositor, LLM provider) deferred; responsibilities pinned first | The large components are the risky ones; contract first, choose later
2026-08-05 | D019 | Working hypothesis: deterministic programmatic rendering beats generative video on both cost and diagram accuracy | Recorded as a hypothesis to test, not yet a decision
2026-08-05 | D020 | Out of scope for draft 1: auth, rate limiting, multi-tenancy, cancellation, artifact GC, horizontal workers | The brief penalises maximising surface area
2026-08-05 | D021 | Round-2 input context captured verbatim in `docs/notes.md`; round-1 docs relocated to `docs/challenges/` | Input, decisions, and design stay in separate files so none of them quietly rewrites another
2026-08-05 | D022 | Design drafts for round 2 live in `docs/plan/` and are reviewed before any code | The reviewer picks scope from `plan/10-scope-matrix.md`; building first would presume the answer
2026-08-05 | D023 | Generation moves out of process: an external agent worker behind `WorkerBroker` and `GenerationBackend` ports; supersedes the in-process provider pipeline in `challenges/01-architecture.md` | The real system rents a machine and runs an agent there; modelling it as a local function call hides every interesting failure
2026-08-05 | D024 | Six subdomains classified core / supporting / generic, and effort follows that classification | Core gets real code and tests, generic gets an interface and a fake
2026-08-05 | D025 | `VideoJob` and `WorkflowRun` are separate aggregates | The job is the public face and rarely written; the run is written every step and heartbeat
2026-08-05 | D026 | `LessonBrief` is sealed, hashed, and write-once; user intent travels downstream only as a Brief | The exact bytes that reached a worker stay reproducible from the record
2026-08-05 | D027 | `SanitisedText` is a distinct type produced only by the intake sanitiser | Skipping sanitisation becomes a type error instead of a review question
2026-08-05 | D028 | Learner context arrives as a typed `ContextItem` list with a closed `kind` enum, never free prose | Free-form context is how injection arrives dressed as data
2026-08-05 | D029 | No-prompt-role rule: user text is delivered as fenced data inside files we author; agent instructions live only in the pinned template repo | User text must never occupy an instruction position
2026-08-05 | D030 | Generation is pull-only: the worker never calls the backend, never writes our storage, and never sits on the serving path | Removes the entire class of inbound-from-untrusted-network problems
2026-08-05 | D031 | The worker's own check results are a claim, not evidence; the backend re-runs verification on the bytes it received | A worker reporting success on a blank video must fail, and the disagreement is itself a metric
2026-08-05 | D032 | Harvest is restricted to an allowlisted `out/` path set, with size and count caps enforced while streaming | Blocks traversal, symlink, and resource-exhaustion families at one checkpoint
2026-08-05 | D033 | Failed artifact candidates are quarantined with a record, not deleted | Operators need the evidence; clients must not be able to reach it
2026-08-05 | D034 | Workflow engine written in-repo rather than adopting a framework, with a step contract shaped for a later swap to durable execution | The failure semantics are the thing being demonstrated
2026-08-05 | D035 | Steps are typed in and out, idempotent, and classify their own failures as RETRYABLE / DEGRADED / TERMINAL / CANCELLED | The engine must not guess intent from exception types
2026-08-05 | D036 | Checkpoints keyed `(run_id, step_name)` with an `input_digest`, and `workflow_version` pinned per run | Resume stays safe across a deploy instead of splicing incompatible halves
2026-08-05 | D037 | `teardown` runs on every exit path, and lease TTLs are ordered worker > generation deadline > run lease | A leaked worker machine is the most expensive bug this system can have
2026-08-05 | D038 | Intake runs inside the workflow, not in the API handler, so a refusal still produces a job record, events, and a metric | A refusal an operator can count beats a bare 422
2026-08-05 | D039 | Queue is at-least-once with idempotent consumers; exactly-once is not attempted | Pretending otherwise moves the problem somewhere less visible
2026-08-05 | D040 | One `JobEvent` stream feeds the job projection, the user progress feed, and the metrics sink | Three separate mechanisms is how a status endpoint and a dashboard end up disagreeing
2026-08-05 | D041 | `visibility: USER \| OPERATOR` is set at the emit site, and the operator `detail` field is never serialised to a client | The emitter knows the audience; a filter maintained elsewhere drifts
2026-08-05 | D042 | Progress percent is monotonic within a job; a retry bumps `attempt` and holds the percent | A bar that reverses reads as broken even when recovery is working
2026-08-05 | D043 | Observability failures never block a job; events buffer and then drop with a counter | Otherwise a monitoring outage becomes a service outage
2026-08-05 | D044 | A red-team fixture corpus asserts which layer stops each attack, not merely that it was stopped | A defence that silently stops working must not be able to hide behind a later one
2026-08-05 | D045 | Every port carries one contract test suite run against every adapter, including the fakes | This is what makes heavy stubbing safe rather than decorative
2026-08-05 | D046 | Deferred work is marked `@TODO` in code and unproven security assumptions `@audit` | The gaps stay visible in the codebase, not only in the docs
2026-08-05 | D047 | Review round 2b captured in `docs/notes.md` and triaged in `plan/11-triage.md` before any doc was edited | The reasoning behind a moved decision is otherwise lost in the diff
2026-08-05 | D048 | SQL (Postgres) is the only source of truth for state, events, costs, and the queue; supersedes D006 | One truth means the API, the dashboard, and an after-the-fact inspection cannot disagree
2026-08-05 | D049 | In-memory repositories demoted to test doubles, kept honest by the same contract suite as the SQL adapters | They were the demo's persistence under D006; now they are a speed optimisation for unit tests
2026-08-05 | D050 | Queue is a `work_items` table claimed with `SELECT ... FOR UPDATE SKIP LOCKED`, not a broker | At-least-once, leases, and dead-letter counting for free, committing in the same transaction as the state it guards
2026-08-05 | D051 | Transactional outbox promoted from deferred to built | Nearly free once a database exists, and it is what stops a status write and its event from diverging
2026-08-05 | D052 | Artifact bytes stay out of the database; the long-term object store holds files, SQL holds metadata and a pointer | Source of truth is about state, not blobs; a 5 MB row makes every backup and query worse
2026-08-05 | D053 | Docker compose with four services: db, storage, api, worker; one image, two entrypoints | Makes the API/runner split real and makes "restart mid-job and watch it resume" a one-command demonstration
2026-08-05 | D054 | Metrics and observation read from SQL; the metrics adapter inserts rows, no console registry and no OTel this round | Observation depending on a second system is how a dashboard and an endpoint start disagreeing
2026-08-05 | D055 | `Idempotency-Key` is required, and job, run, session, step-effect, and artifact ids are all derived from it via uuid5 | Deduplication becomes a primary-key collision instead of a read-then-write race, and at-least-once becomes safe by construction
2026-08-05 | D056 | Token cost metered inside every workflow step, with a precondition check before and a debit after | Abuse prevention has to stop the next call, which a report written at the end cannot do
2026-08-05 | D057 | Three cost ceilings: per job, per chat context, per principal per day | The per-context ceiling is what stops one conversation regenerating forever, which is the shape a chat frontend invites
2026-08-05 | D058 | Cost entries carry `source: MEASURED \| CLAIMED`; worker-reported usage is never mixed with metered usage | The agent runs on its own credentials in its own repo, so its numbers are untrusted input; ceilings on that step are enforced by wall clock and iteration cap instead
2026-08-05 | D059 | `chat_context_id` is a first-class dimension on jobs and artifact listings, indexed but not owned | The frontend is a chat product; modelling the conversation would pull chat concerns into a video service
2026-08-05 | D060 | A chat context id supplied by a client is checked against the principal before any read scoped by it | Otherwise one user enumerates another's artifacts by guessing an id
2026-08-05 | D061 | The brief is rendered through a versioned template registry, and `template_version` is part of the brief hash | Prompt quality becomes a reviewable diff and a regression can be bisected by re-rendering
2026-08-05 | D062 | `domain/errors.py` holds one `ErrorCode` enum and one `ERROR_CATALOG`; no message is written at a raise site | One place to audit for leaks, one place to translate later
2026-08-05 | D063 | Every step validates its input and its output, and nothing from outside the process reaches step logic as a raw dict | The two untrusted directions are the submitted request and whatever a worker returns
2026-08-05 | D064 | `ResultValidator` is a versioned port, and its version is recorded on every artifact | Upgrading validation becomes a swap comparable against the old verdicts, not an edit spread through custody
2026-08-05 | D065 | No step imports another step; steps share types from `domain/` only, enforced by an import-linter contract in CI | An architectural rule that is not checked is a comment
2026-08-05 | D066 | Every data table has exactly one module allowed to write it; everyone else goes through that module's port | "Who wrote this row" stays answerable, and job status stays checkable in one place
2026-08-05 | D067 | `AccessScope` can only be constructed by the `access` module, and every repository read method requires one | An unscoped listing query becomes a missing argument rather than a review finding
2026-08-05 | D068 | Chat context ownership is claim-on-first-use, recorded in `chat_context_grants` | Smallest authorisation model that stops one learner reading another's artifacts; safe only while context ids are unguessable
2026-08-05 | D069 | The worker receives `session_id` and `trace_id` and never `job_id`, `run_id`, `principal_id`, or `chat_context_id` | A worker holding a session id cannot address anything else in the system
2026-08-05 | D070 | Admin surface for draft 1 is the database and the logs, not an API | Building an admin API before the product exists is the wrong order; the gap is stated rather than discovered
2026-08-05 | D071 | SQL schema and the `/v1` API contract are frozen in `plan/13-mvp.md`; everything else may be rewritten behind a port | These two are the only pieces whose change costs a migration or a client release
2026-08-05 | D072 | Upgradability rules: opaque prefixed ids, text enums, typed columns for stable fields and JSONB for volatile ones, integer micros, `timestamptz`, additive-only `/v1`, opaque cursors | Today's demo shapes have to survive into production rather than being thrown away
2026-08-05 | D073 | `chat_context_id` denormalised onto `artifacts`, with a partial index excluding quarantined rows | The chat panel listing becomes one index scan with no join
2026-08-05 | D074 | MVP is stages 0 to 4 in `plan/13-mvp.md`: submit, poll, finish, list by context, download | Anything off the path from API call to retrievable output is not MVP
2026-08-05 | D075 | `docker-compose.yml` lives at the repository root and infrastructure assets under `infra/`; supersedes the `deploy/` path in `plan/03-module-layout.md` | The compose file is the entry point a reviewer looks for first, and it should not be one directory down
2026-08-05 | D076 | One image built by `uv sync --locked` from a multi-stage Dockerfile, run as two commands: `uvicorn app.main:app` and `python -m app.worker` | D053's "same code, different role" has to be one build artifact or it is two services that drift
2026-08-05 | D077 | Postgres driver is `psycopg` 3, and the object store is reached over the S3 API with `boto3` | MinIO now and S3 later is one client and no adapter rewrite
2026-08-05 | D078 | `Makefile` exposes `up` and `down` and nothing else | A build script that grows verbs becomes a second, undocumented interface to the system
2026-08-05 | D079 | `import-linter` contracts live in `pyproject.toml` and start with three: domain imports nothing, api reaches no adapter, orchestration imports no adapter | D065 needs a file that fails a build, not an intention
2026-08-05 | D080 | Repository spine committed with no application logic: packages carrying only docstrings, two entrypoints, `/health`, and compose | Stage 1 of `plan/13-mvp.md` is checkable on its own, and every later stage lands against a structure that already runs
