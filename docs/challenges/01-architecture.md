# Architecture (draft 1)

Status: design only. No implementation exists yet. Tech choices for the media path are
deliberately deferred, see `02-components.md` and `open-questions.md`.

## Shape of the system

Four boundaries, as demanded by R8. Everything else is an implementation detail behind them.

```
                 ┌──────────────────────────────────────────────┐
   HTTP client   │                  API layer                   │
  (curl/Postman) │  request validation, serialisation, no logic │
        │        └───────────────┬──────────────────────────────┘
        │                        │ commands / queries
        ▼                        ▼
   POST /v1/jobs        ┌─────────────────────┐      ┌──────────────────┐
   GET  /v1/jobs        │    Job service      │─────▶│   Job store      │
   GET  /v1/jobs/{id}   │  lifecycle rules,   │      │  (Repository)    │
   GET  /v1/jobs/{id}/  │  state transitions  │◀─────│  in-mem → SQL    │
        artifact        └──────────┬──────────┘      └──────────────────┘
                                   │ enqueue
                                   ▼
                        ┌─────────────────────┐
                        │      Dispatcher     │  in-process asyncio task
                        │      (Queue)        │  → external broker later
                        └──────────┬──────────┘
                                   │ run(job)
                                   ▼
                        ┌─────────────────────────────────────────┐
                        │           Pipeline orchestrator         │
                        │  deterministic control flow, retries,   │
                        │  fallbacks, gates, cost + event capture │
                        └──┬──────┬───────┬────────┬──────────┬───┘
                           │      │       │        │          │
                        plan   validate  visuals  audio    compose
                           │      │       │        │          │
                           ▼      ▼       ▼        ▼          ▼
                        ┌─────────────────────────────────────────┐
                        │        Provider adapters (ports)        │
                        │  LLM · renderer · TTS · muxer           │
                        │  each swappable, each mockable          │
                        └────────────────────┬────────────────────┘
                                             │ write
                                             ▼
                                   ┌──────────────────┐
                                   │  Artifact store  │
                                   │  local FS → S3   │
                                   └──────────────────┘
```

The API layer never touches a provider. The orchestrator never touches HTTP. The job store
holds state, the artifact store holds bytes, and they are different things on purpose.

## Job lifecycle

Coarse `status` for clients, fine `stage` for humans and debugging. Two fields instead of
one twelve-value enum keeps the client contract stable while the pipeline changes.

```
                          ┌──────────┐
   POST /v1/jobs ───────▶ │  QUEUED  │
                          └────┬─────┘
                               │ dispatcher picks up
                          ┌────▼─────┐   stage: PLANNING → PLAN_CHECK → VISUALS
                          │ RUNNING  │           → NARRATION → COMPOSE → QUALITY_GATE
                          └────┬─────┘
             ┌─────────────────┼──────────────────┐
             │                 │                  │
     all gates pass    retryable error     terminal error
             │                 │                  │
        ┌────▼─────┐    (back to RUNNING,   ┌─────▼─────┐
        │SUCCEEDED │     attempt += 1)      │  FAILED   │
        └──────────┘                        └───────────┘
                                            error.code + error.stage set
```

- `QUEUED` — accepted, persisted, not started. Returned by `POST /v1/jobs` with `202`.
- `RUNNING` — a worker owns it. `stage` and `attempt` move underneath.
- `SUCCEEDED` — artifact exists, passed the quality gate, retrievable.
- `FAILED` — terminal. Carries a machine-readable `error.code`, the `stage` that failed, and
  a learner-safe message. Never reached silently (N5).
- `CANCELLED` — optional, only if a cancel endpoint earns its place.

Transitions are the job service's job, not the orchestrator's. The orchestrator reports
"stage X finished / stage X failed with reason R"; the service decides what that means for
the status.

Invariant: a job in `SUCCEEDED` always has a readable artifact. The quality gate runs
before the transition, not after.

## Request flow, end to end

1. `POST /v1/jobs` with `{"query": "How does the pH scale work?"}`.
2. API validates shape, computes `content_key = hash(normalised_query, pipeline_version)`.
3. Job service checks for an existing `SUCCEEDED` job with the same `content_key`. On hit,
   it returns that artifact reference immediately. This is the cache that makes repeat runs
   cheap and identical (N1, N3).
4. On miss: persist job as `QUEUED`, enqueue, return `202` with `job_id` and `Location`.
5. Dispatcher runs the pipeline. Each stage writes its output keyed by
   `(job_id, stage, attempt)`, so a resumed job re-uses completed stages instead of paying
   for them twice.
6. Client polls `GET /v1/jobs/{id}` and sees `status`, `stage`, `attempt`, `progress`,
   `cost_estimate`, and `error` when relevant.
7. On success, `GET /v1/jobs/{id}/artifact` streams or redirects to the mp4.

## Persistence boundary

Two stores, two protocols, both trivially swappable.

`JobRepository` — create, get, list (paged, filterable by status), update state with an
optimistic-concurrency guard. First implementation is an in-memory dict behind an async
lock. The brief permits this (Explicit permissions). The protocol shape is chosen so a
SQLAlchemy or asyncpg implementation is a drop-in.

`ArtifactStore` — put(bytes|path, metadata) → `artifact_id`, get metadata, open stream,
build URL. First implementation writes to `./data/artifacts/{artifact_id}/` with a
`manifest.json` next to the media. Content-addressed by `content_key`, so identical inputs
map to the same artifact instead of re-rendering.

Job records reference artifacts by id. They never embed bytes.

## AI / video generation boundary

Every non-deterministic external call sits behind a narrow port with a typed request and a
typed response. The orchestrator only knows the ports.

- `ConceptPlanner` — query → `LessonPlan` (structured, schema-validated).
- `SceneRenderer` — `Scene` → visual asset(s) + timing manifest.
- `NarrationSynthesizer` — narration text → audio track + word timings.
- `Compositor` — visuals + audio + timings → final mp4.

Each port has at least two implementations from day one: the real provider and a
deterministic fake. The fake is what makes tests fast and what proves the boundary is real
rather than decorative (N8).

## Extending to other STEM topics

The only chemistry-specific pieces are the planner's prompt/template pack and the visual
primitive library (pH strip, atom, bond, electron cloud). Both live in a `subjects/`
registry keyed by subject id. Adding physics means adding a template pack and a primitive
set, not touching the job service, the API, or the orchestrator.

## What is deliberately out of scope for draft 1

Auth, multi-tenancy, rate limiting, webhooks/callbacks, horizontal workers, streaming
progress over SSE/WebSocket, cancellation, artifact TTL/GC. Each is a note in
`open-questions.md` rather than code.
