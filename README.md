# growtrics-challenge

Demo backend for an AI chemistry video request service. A user asks for a lesson, gets a job id back, polls it, and downloads an MP4.

- Backend (this repo): https://github.com/VAD37/growtrics-challenge
- Agent sandbox, where the video is generated: https://github.com/VAD37/growtrics-llm-engine

Check `docs/` folder for full project architecture design and decisions.

## Setup

Needs Docker + python uv. Everything else is optional.

```bash
cp .env.example .env    # every value is also a compose default, so this is optional
make up                 # build and start db, storage, api, worker
make demo               # run the whole walkthrough against localhost:8000
make down               # stop them
```

## Notes for interviewer

This project does not generate a video by itself.
The demo showcases the backend API, spawning the llm agent, mock gathering the result from LLM cloud sandbox, and mock video generation.

The video generation service is test/generated from a [different repo](https://github.com/VAD37/growtrics-llm-engine).
It just a local terminal LLM follow command to generate a video artifacts to certain folder where backend worker can check/validate result before upload to storage.
Here is system prompt that I run on local claude terminal inside `growtrics-llm-engine` repo.

```
You are an agent that produces STEM explainer animation, and you are an expert in Manim — the mathematical animation engine that renders video programmatically from Python.

Read examples context,readme and spawn subagents to generate video.
```

The rest of this `README.md` is LLM generated.

---

### Videos

| Lesson                            | Runs   | In this repo                                                                        | In the agent sandbox                                                                                               |
| --------------------------------- | ------ | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| Why do atoms form covalent bonds? | 81.2 s | [covalent_bonds.mp4](src/app/generation/backends/fixtures/covalent_bonds.mp4)       | [covalent-bonds/final.mp4](https://github.com/VAD37/growtrics-llm-engine/blob/main/covalent-bonds/final.mp4)       |
| Ionic versus covalent bonding     | 88.6 s | [ionic_vs_covalent.mp4](src/app/generation/backends/fixtures/ionic_vs_covalent.mp4) | [ionic-vs-covalent/final.mp4](https://github.com/VAD37/growtrics-llm-engine/blob/main/ionic-vs-covalent/final.mp4) |
| How does the pH scale work?       | 65.6 s | [ph_scale.mp4](src/app/generation/backends/fixtures/ph_scale.mp4)                   | [ph-scale/final.mp4](https://github.com/VAD37/growtrics-llm-engine/blob/main/ph-scale/final.mp4)                   |

### Jobs pipeline

This is what a job should look like:

1. Receive User API calls with context
2. Create a job. Store it in job inbox. User get jobID and can query status from API
3. Backend worker pull latest job in queue from inbox.
4. Some basic LLM prompt guard to verify job is not prompt injection/attack. (optional)
5. Backend spawn/find a worker to run job task
6. A worker clone template github repo. load context + predefined system prompt
7. template should include some basic iteration and test harness to check artifacts output follow standard
8. Backend see report of worker done task. Scan for artifacts and run another LLM validation on videos and script (optional)
9. Backend store artifacts in DB, persistance storage. Update job status to done.
10. User can query GET job artifacts

## API

Demo include six endpoints. Identity is the `X-User-Id` header on every request, auth is skipped.

| Method | Path                                  | Purpose                                                                           |
| ------ | ------------------------------------- | --------------------------------------------------------------------------------- |
| GET    | `/health`                             | is the database reachable                                                         |
| POST   | `/v1/jobs`                            | submit a new request, get `202` and a job id, have rate limit when server is full |
| GET    | `/v1/jobs/{job_id}`                   | status, stage, percent, and the artifact summary once it succeeds                 |
| GET    | `/v1/jobs`                            | list caller's jobs, newest first, cursor paged                                    |
| GET    | `/v1/artifacts`                       | video artifacts, optional `?job_id=` filter                                       |
| GET    | `/v1/artifacts/{artifact_id}/content` | streaming bytes to file                                                           |

`POST /v1/jobs` takes an optional `Idempotency-Key`. Same key and same body returns the same job, same key and a different body is `409`. Absent, the server mints one and that call has no replay protection.

## What inside Demo

End-to-end test from user calls -> server handling request inbox -> mock spawn agent -> mock fake video artifacts -> give back jobs to user

## Layout

One line each, and this is all of `src/app/`:

| Module           | What it does                                                                                                            |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `main.py`        | API entrypoint and composition root: builds the adapters, overrides the ports, mounts the routers                       |
| `worker.py`      | worker entrypoint: claim one work item under a lease, run it, complete it, repeat, and sweep abandoned leases beside it |
| `config.py`      | settings, and the only reader of the environment                                                                        |
| `api/`           | routers and Pydantic schemas, no domain rules, reaches no adapter                                                       |
| `domain/`        | ids, statuses, stages, failure codes, the error catalog. Imports nothing                                                |
| `orchestration/` | the job use cases, and the workflow engine that drives intake to generation to custody                                  |
| `intake/`        | sanitise the raw request, seal it into a `LessonBrief`, render the engine's `context.json`                              |
| `generation/`    | the anti-corruption layer over the engine: a port, with `MockGenerationBackend` behind it                               |
| `custody/`       | harvest the run directory, verify the bytes against the output contract, store them, serve them                         |
| `access/`        | `X-User-Id` to `Principal` to `AccessScope`. A stub, one function to replace                                            |
| `observability/` | events and metrics. Cut from the demo, designed in `docs/plan/08`                                                       |
| `storage/`       | the adapters: `sql/`, `objects/`, and `memory/` test doubles                                                            |

### Job lifecycle

1. `POST /v1/jobs`. The API validates, checks admission (3 active jobs per principal, else `429`),
   derives `job_id` from `uuid5(principal, context, idempotency key)`, then inserts the job row and
   its `work_items` row in one transaction. `202`.
2. The worker claims the oldest ready work item with `FOR UPDATE SKIP LOCKED` under a lease, and a
   heartbeat renews that lease at a third of its length. A long render keeps its claim without
   holding a long lease.
3. `intake` sanitises the instruction and context, seals a `LessonBrief` (a row of its own: what we
   asked for, not what the user typed), and writes `context.json`.
4. `generation` starts a run and waits for `out/result.json`.
5. `custody` harvests the output directory against a path allowlist and a size cap, runs the
   `video.short.v1` checks, writes the bytes to object storage, and inserts the artifact rows.
6. `orchestration` moves the status, and only it does: `QUEUED/INTAKE 10%` to
   `RUNNING/GENERATING 60%` to `SUCCEEDED/DONE 100%`, off a static stage-to-percent map.
7. Failure sets `failure.code`, status `FAILED`, and the work item is not retried. A crash never
   releases the lease. It expires, and the sweeper finds it.

The client polls `GET /v1/jobs/{id}` throughout. Once the job succeeds that response carries the
primary artifact summary, so the client has a `content_url` without a second call.

### Persistence and artifact boundary

Postgres is the only source of truth (D048). Six tables: `principals`, `requests`, `briefs`,
`jobs`, `work_items`, `artifacts`. The queue is a table rather than a broker, which is what lets
submit write the job and its work item in one transaction with no outbox.

Object storage holds bytes and nothing else. It has no opinion on who owns a file and no row of
its own. The `artifacts` row carries the identity (`principal_id`, `job_id`, role, media type,
sha256, size) and a key, the bucket carries the megabytes. Reads go through the API, which checks
scope and then streams from the bucket. No presigned URL escapes, so revoking access stays a
database question.

Nothing is published before `custody` verifies it. A row that fails its checks is quarantined and
excluded from every listing by a partial index, rather than deleted.

### AI/video-generation boundary

The engine is a separate repository with its own decision log. Media tech (renderer, TTS, LLM
provider, agent template) is chosen there and deliberately unchosen here.

The seam is a directory, described in `docs/plan/15-engine-seam.md`. We write one
`<run-dir>/context.json`, poll for `<run-dir>/out/result.json`, harvest it, verify it, then
destroy the directory. We never tell it how to generate.

What crosses is derived from the sealed brief, never from the raw request. The only identity that
crosses is a one-way hash of our run id: no `job_id`, no `principal_id`, no idempotency key. There
is no field to put them in.

Today `generation` is wired to `MockGenerationBackend`, which returns a manifest and a real
committed lesson video in the shape the seam defines. Swapping in the engine replaces one class
behind the port and touches no schema (D101).

### The plan behind all this

`docs/` is the source of truth, and it is longer than the code.

| Doc                                  | Contents                                                                                       |
| ------------------------------------ | ---------------------------------------------------------------------------------------------- |
| `docs/demo.md`                       | the approved scope: six endpoints, six tables, four stages, and what each cut costs to restore |
| `docs/plan/README.md`                | start here for the design, which is a superset of what got built                               |
| `docs/plan/12-data-control.md`       | the system in one diagram                                                                      |
| `docs/plan/13-mvp.md`                | the frozen SQL schema and `/v1` contract that `demo.md` subsets                                |
| `docs/plan/15-engine-seam.md`        | how we talk to the video engine                                                                |
| `docs/decisions.md`                  | append-only decision log, one line each. Every `D0xx` above resolves here                      |
| `docs/open-questions.md`             | deferred choices, and what closes each                                                         |
| `docs/challenges/00-requirements.md` | the brief extracted from the PDF, with stable ids                                              |
