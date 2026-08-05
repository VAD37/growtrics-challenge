# Observability

One event stream in one database, three readers. The job projection, the metrics rollups, and
the user-facing progress feed all derive from the `job_events` table. Building them separately
is how a status endpoint and a dashboard end up disagreeing about whether a job failed.

```
   step boundaries ──▶                    ┌──▶ job projection  (GET /v1/jobs/{id})
   port calls      ──▶  job_events (SQL) ─┼──▶ user feed       (GET /v1/jobs/{id}/events)
   sweeper actions ──▶                    └──▶ metric rollups  (GET /internal/metrics)
```

SQL is the source of truth for observation as well as for state, which removes the usual
question of whether the dashboard and the API are looking at the same thing. Events are
written in the same transaction as the state change that produced them, through the outbox in
`07-distribution.md`.

## Event shape

```python
JobEvent(
    seq: int,                # dense, per job
    job_id: JobId,
    run_id: RunId | None,
    at: datetime,
    type: EventType,
    stage: StageName,
    attempt: int,
    severity: INFO | WARN | ERROR,
    visibility: USER | OPERATOR,
    message: str,            # already safe to display
    detail: dict,            # operator only, never serialised to a user
    trace_id: TraceId,
)
```

`visibility` on the event rather than a filter at the read site. The person writing the emit
call is the one who knows whether a learner should see it, and a filter maintained elsewhere
drifts.

`message` is written for the learner and `detail` for the operator. There is no path that
serialises `detail` to a client, and that is asserted in a test, because it is the field where
a stack trace or a raw model response would otherwise leak.

## What the user sees

Requirement N7 and the stated core both want the user to observe their own task. The demo
gives them three things:

1. **Status document.** Where the job is now: `status`, `stage`, `progress.percent`,
   `progress.message`, `attempt`, and `error` when terminal.
2. **Event feed.** How it got there, from a cursor, user-visible events only.
3. **Honest failure.** A terminal job carries a code, the stage that failed, and a sentence
   that says what happened without exposing internals.

Progress rules that make the display trustworthy:

- Monotonic percent. Retries bump `attempt` and hold the percent. A bar that reverses reads as
  a broken system even when recovery is working.
- Coarse and honest. Percent is derived from the step cursor with a fixed weight per step, not
  interpolated inside a long-running step. During `generate`, the percent holds and the event
  feed carries the movement.
- Stage names are a stable, small vocabulary. Step names may change freely.
- Every terminal state has a message. Nothing ends silently (N5).

## Metrics

`MetricsPort` with `counter`, `gauge`, `histogram`, and a `timer` context manager. The adapter
writes rows to a `metric_events` table. Nothing fancy: no agent, no scrape endpoint, no
time-series database. `GET /internal/metrics` runs aggregate queries over it.
`@TODO` OpenTelemetry behind the same port, with no call site changes.

`@audit` computing rollups by querying raw events does not survive past a demo. The fix is a
rollup table written by the outbox relay, not a more sophisticated sink. Marked, not built.

Names and labels fixed up front, because renaming a metric after dashboards exist is
expensive:

| Metric | Type | Labels |
|--------|------|--------|
| `jobs_submitted_total` | counter | `subject`, `outcome` |
| `jobs_terminal_total` | counter | `status`, `failure_code` |
| `job_duration_seconds` | histogram | `status` |
| `step_duration_seconds` | histogram | `step`, `outcome` |
| `step_attempts_total` | counter | `step`, `classification` |
| `guard_decisions_total` | counter | `decision`, `rule_family` |
| `worker_placement_seconds` | histogram | `backend`, `outcome` |
| `generation_iterations` | histogram | `outcome` |
| `artifacts_verified_total` | counter | `verdict`, `reason` |
| `worker_claim_mismatch_total` | counter | `check` |
| `cost_micros_total` | counter | `step`, `source` |
| `budget_exhausted_total` | counter | `ceiling` |
| `queue_depth` | gauge | none |
| `active_runs` | gauge | none |
| `active_worker_leases` | gauge | `backend` |

`worker_claim_mismatch_total` is the interesting one. It counts the times a worker reported
its output as qualified and our independent verification disagreed. It is the direct measure
of whether the generation side can be trusted, and it only exists because verification is
duplicated on purpose.

## Signals worth alerting on

Not built this round, but named so the metrics above are the right ones:

- Terminal failure rate above a threshold over a window.
- p95 time to artifact, split by whether the run degraded.
- Quarantine rate and claim-mismatch rate, either of which rising means the generation side
  changed underneath us.
- Active worker leases above active runs, which means machines are leaking.
- Queue depth rising while active runs is flat, which means runners are stuck rather than
  busy.

## Tracing and correlation

A `trace_id` is minted at submission and carried on every event, log line, and step. It is
written into the worker workspace as `.trace`, and the template repo's skills are expected to
echo it into their own logs. That is what lets a harvested agent log be joined to the backend
timeline without trusting anything else the worker said.

Logs are structured, one JSON object per line, always carrying `trace_id`, `job_id`, and
`step`. User-authored text is never logged verbatim: the log carries a hash and a truncated
prefix, so an operator can correlate without the log becoming a copy of everything anyone ever
typed.

`@TODO` span-based tracing. The trace id and the step boundaries are already the right shape
for it; the export is the missing piece.
