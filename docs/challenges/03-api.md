# API design (draft 1)

Versioned under `/v1`. JSON in, JSON out, except the artifact stream. OpenAPI docs at
`/docs` serve as the demo client (the brief allows this).

## Endpoints

| Method | Path | Purpose | Req |
|--------|------|---------|-----|
| `POST` | `/v1/jobs` | Request a video for a learner query | R2 |
| `GET` | `/v1/jobs` | List jobs, paged and filterable | R4 |
| `GET` | `/v1/jobs/{job_id}` | Job status detail | R5 |
| `GET` | `/v1/jobs/{job_id}/events` | Stage timeline for the job | N7 |
| `GET` | `/v1/jobs/{job_id}/artifact` | Stream or redirect to the mp4 | R6 |
| `GET` | `/v1/jobs/{job_id}/plan` | The `LessonPlan` used, for inspection | N7 |
| `GET` | `/v1/concepts` | Supported concepts, for discoverability | — |
| `GET` | `/health` | Liveness | — |

## POST /v1/jobs

```jsonc
// request
{ "query": "How does the pH scale work?" }

// 202 Accepted, Location: /v1/jobs/job_01J...
{
  "job_id": "job_01J...",
  "status": "queued",
  "stage": null,
  "query": "How does the pH scale work?",
  "concept_id": "ph_scale",
  "created_at": "2026-08-05T10:00:00Z"
}
```

`200 OK` instead of `202` when an identical `content_key` already has a `SUCCEEDED` job:
the existing job is returned and nothing is generated. Cheap, repeatable, and it makes the
cache visible in the contract rather than hidden in a log line.

`422` for a query that maps to no supported concept, with the supported list in the error
detail. Failing loudly at the door beats generating a plausible video about the wrong thing.

## GET /v1/jobs/{job_id}

```jsonc
{
  "job_id": "job_01J...",
  "status": "running",              // queued | running | succeeded | failed
  "stage": "visuals",               // planning | plan_check | visuals | narration
                                    // | compose | quality_gate | null
  "attempt": 1,
  "progress": { "completed": 3, "total": 6 },
  "query": "How does the pH scale work?",
  "concept_id": "ph_scale",
  "artifact": null,                 // set when succeeded
  "cost_estimate_usd": 0.0042,
  "error": null,
  "created_at": "...", "started_at": "...", "updated_at": "...", "finished_at": null
}
```

Succeeded adds:

```jsonc
"artifact": {
  "artifact_id": "art_...",
  "url": "/v1/jobs/job_01J.../artifact",
  "content_type": "video/mp4",
  "duration_s": 62.4,
  "size_bytes": 4183992,
  "checksum": "sha256:..."
}
```

Failed adds:

```jsonc
"error": {
  "code": "PLAN_VALIDATION_FAILED",
  "stage": "plan_check",
  "message": "Generated plan did not cover the requested concept.",
  "attempts": 3,
  "retryable": false
}
```

## GET /v1/jobs

Query params: `status`, `concept_id`, `limit` (default 20, max 100), `cursor`.
Returns `{"items": [...], "next_cursor": "..."}` with the same job objects, artifact
included when present.

## GET /v1/jobs/{job_id}/events

The waiting state made legible. One entry per stage attempt.

```jsonc
{"events": [
  {"at": "...", "stage": "planning",   "status": "started",   "attempt": 1},
  {"at": "...", "stage": "planning",   "status": "succeeded", "attempt": 1,
   "duration_ms": 2840, "cost_usd": 0.0018},
  {"at": "...", "stage": "visuals",    "status": "failed",    "attempt": 1,
   "duration_ms": 900, "error_code": "RENDER_TIMEOUT", "retryable": true},
  {"at": "...", "stage": "visuals",    "status": "started",   "attempt": 2}
]}
```

## GET /v1/jobs/{job_id}/artifact

`200` with `video/mp4` and range support, or `302` to the store URL once the store is
remote. `409` when the job is not `succeeded`, carrying the current status so a polling
client knows whether to keep waiting or give up.

## Error envelope

Every non-2xx uses one shape:

```jsonc
{ "error": { "code": "JOB_NOT_FOUND", "message": "...", "details": {} } }
```

Codes are a closed enum, grouped by stage, so a client can branch on them. Draft 1 set:
`INVALID_QUERY`, `CONCEPT_NOT_SUPPORTED`, `JOB_NOT_FOUND`, `ARTIFACT_NOT_READY`,
`PLAN_GENERATION_FAILED`, `PLAN_VALIDATION_FAILED`, `RENDER_FAILED`, `NARRATION_FAILED`,
`COMPOSE_FAILED`, `QUALITY_GATE_FAILED`, `PROVIDER_UNAVAILABLE`, `INTERNAL_ERROR`.

## Polling contract

No webhooks or SSE in draft 1. Clients poll `GET /v1/jobs/{id}`. The response carries
`Retry-After` while `queued`/`running` so a client has a sanctioned poll interval instead
of a guess.
