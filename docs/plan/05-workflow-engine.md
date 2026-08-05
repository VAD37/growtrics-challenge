# Workflow engine

The declared core of the system. A run must be able to lose the process, the worker, or any
single step, and still end in a state a human can explain.

Written as a small engine rather than pulled from a workflow framework, because the failure
semantics are the thing being demonstrated. `@audit` for production this is where Temporal
or a durable-execution service earns its keep; the step contract below is shaped so that
swap does not rewrite the steps.

## Step contract

```python
class Step(Protocol):
    name: StepName
    stage: StageName              # what the client sees while this runs
    retry: RetryPolicy
    timeout_s: float
    compensation: Compensation | None

    async def run(self, ctx: StepContext, inp: StepInput) -> StepOutput: ...
```

`StepContext` carries `job_id`, `run_id`, `attempt`, `trace_id`, `deadline`, the event
emitter, and the ports the step needs. It does not carry the whole application.

Rules every step obeys:

1. **Typed in, typed out, validated both ways.** Both sides are Pydantic models and the engine
   validates on the way in and on the way out. A step cannot quietly hand the next one a shape
   it did not expect, and nothing arriving from outside the process reaches step logic
   unvalidated. This applies hardest to the two untrusted directions: the submitted request
   and anything a worker returns. Neither is a `dict` by the time a step sees it.
2. **Idempotent.** Given the same `(run_id, name, attempt_group)` it may be executed twice
   with the same observable result. External effects are keyed by an idempotency key derived
   from those fields.
3. **No hidden state.** Everything the next step needs is in the returned `StepOutput`, which
   gets persisted as a checkpoint. A step may not stash things on the run object.
4. **Declares its own failures.** A step raises `StepFailure(code, classification, detail)`,
   where `code` is an `ErrorCode` from the catalog in `domain/errors.py`. No step writes a
   message. The engine does not guess from exception types.
5. **Declares its cost estimate.** `step.estimate(inp) -> CostEstimate`. The engine checks it
   against the remaining budget before running, and debits the actual afterwards.

Classifications:

| Classification | Engine behaviour |
|----------------|------------------|
| `RETRYABLE` | Backoff and retry within the step's policy and the run's budget |
| `DEGRADED` | A fallback exists. Take it, mark the run degraded, continue |
| `TERMINAL` | Stop, compensate backwards, fail the job with this code |
| `CANCELLED` | Stop, compensate backwards, mark cancelled |

Anything uncaught is treated as `RETRYABLE` once, then `TERMINAL`. An unclassified crash
should not be able to burn a budget in a retry loop.

## The video job workflow

| # | Step | Stage | Retry | Compensation | Failure is |
|---|------|-------|-------|--------------|-----------|
| 1 | `intake` | INTAKE | none | none | terminal |
| 2 | `admit` | ADMISSION | 2, fast | release hold | terminal |
| 3 | `place` | PLACEMENT | 5, slow | release lease | retryable, then terminal |
| 4 | `provision` | PREPARING | 3 | wipe workspace | retryable |
| 5 | `generate` | GENERATING | 2 | close session | retryable, degradable |
| 6 | `harvest` | COLLECTING | 3 | drop candidates | retryable |
| 7 | `verify` | VERIFYING | 1 | quarantine | terminal or degradable |
| 8 | `publish` | PUBLISHING | 3 | unpublish | retryable |
| 9 | `settle` | DONE | 3 | none | retryable, never blocks success |
| 10 | `teardown` | any | 3 | none | always runs, failure only logged |

`teardown` is not a normal step. It runs on every exit path, success or failure or cancel,
and its job is to make sure no worker lease outlives its run. A leaked worker is the most
expensive kind of bug this system can have.

`intake` sits inside the workflow rather than in the API handler so that a rejected request
still produces a job record, an event trail, and a metric. A refusal the user can see and an
operator can count is worth more than a bare `422` with nothing behind it.

## Checkpointing and resume

```
StepRecord(run_id, step_name, attempt, status, input_digest, output, started_at, ended_at, error)
```

Written after every completed step, keyed by `(run_id, step_name)`. On resume the engine
walks the definition from the start, skips any step with a `SUCCEEDED` record whose
`input_digest` matches, and executes from the first that does not.

The `input_digest` check is what makes resume safe across a code change. If the workflow
definition or the brief changed, digests stop matching, and the engine re-runs rather than
splicing incompatible halves together. `workflow_version` is pinned on the run for the same
reason: a run started under v3 finishes under v3 even if v4 deployed mid-flight.

## Surviving each failure

| What fails | Detection | Recovery |
|------------|-----------|----------|
| A step raises | The engine catches it | Classify, retry with backoff, or compensate and fail |
| A step hangs | `timeout_s` per step, deadline on the run | Cancel the step, count as retryable, release resources |
| The worker dies mid-generation | Heartbeat gap on the session poll | Close the session, retry `place` and `provision`, resume from checkpoint |
| The backend process dies | Lease expiry on the run | Sweeper marks the lease dead, requeues the run, resume from the last checkpoint |
| The queue loses a message | Run left in `QUEUED` past a threshold | Sweeper re-enqueues. At-least-once plus idempotent steps makes this safe |
| The artifact store rejects a write | `publish` raises | Retry, then terminal with `PUBLISH_FAILED`; nothing half-published because the pointer swap is last |
| Verification fails | `verify` returns a quarantine verdict | Retry generation once with a tightened brief, then fail with `ARTIFACT_QUARANTINED` |
| Everything retryable is exhausted | Run budget spent | Compensate backwards, fail with the last cause, release the hold |

The rule tying these together: **no path ends without either a published artifact or a
recorded failure, and every path releases what it acquired.** Compensations run in reverse
order of acquisition, each one idempotent, each one allowed to fail without blocking the
others.

## Cost gates

Metering lives at the step boundary, not in a report at the end. The reason is abuse
prevention: a report tells you afterwards that a conversation looped fifty regenerations, a
gate stops the fifty-first.

Around every step the engine does three things:

1. **Precondition.** `remaining = ceiling - spent`. If `step.estimate(inp) > remaining`, the
   run stops with `BUDGET_EXHAUSTED` before doing work it cannot pay for. Compensations still
   run.
2. **Debit.** On return, write a `CostEntry` with the actual usage, marked `MEASURED` when the
   backend metered it and `CLAIMED` when a worker reported it.
3. **Watch the gap.** A step whose actual widely exceeds its estimate is a metric, not a
   crash. Estimates that are consistently wrong make the gate useless in the direction that
   matters.

Three ceilings, checked in order: per job, per chat context, and per principal per day. The
per-context ceiling is the one that stops a single conversation from regenerating forever,
which is the abuse shape a chat frontend invites.

`@audit` `generate` is the expensive step and the one the backend cannot meter, because the
agent runs on its own credentials in its own repository. Its ceiling is enforced by wall clock
and iteration cap, not by tokens. Any budget number for that step is an estimate reconciled
against a claim. See Q-R.

## Retry policy

```python
RetryPolicy(max_attempts=3, base_delay_s=1.0, factor=2.0, max_delay_s=30.0, jitter=0.3)
```

Per step, and bounded again at the run level by `max_total_attempts` and a wall-clock
deadline. Two budgets because a step-local cap alone lets a long workflow retry its way past
any sane cost ceiling.

Jitter is not decoration. Without it, a batch of jobs failing on the same downstream outage
retries in lockstep and rebuilds the outage on recovery.

## Degradation ladder

Applies to `generate` and `verify`. Each rung is cheaper and more predictable than the one
above.

1. Full agent run against the template repo, iterating until its own checks pass.
2. Retry with a tightened brief: shorter duration, fewer scenes, stricter constraints.
3. `@TODO` a reference plan for the concept, rendered by a fixed script with no agent in the
   loop. Guarantees the three brief queries always produce something.
4. Fail cleanly with a message naming what the learner can do next.

Rung 3 is what turns "the agent is flaky" from an outage into a quality dip. It is on the cut
list because it costs real work; without it the ladder ends at rung 2 and the failure rate
is whatever the agent's is.

## Testing the engine

The engine is testable without any of the real subsystems, and that is the main argument for
writing it as its own module:

- A workflow of fake steps, each scripted to fail in one classification, asserting the
  resulting job status, event sequence, and compensation order.
- Crash injection: kill the runner between any two steps, resume, assert no step ran twice
  with a visible effect and the run still finishes.
- Duplicate delivery: hand the same work item to two runners, assert one wins the lease and
  the other exits without side effects.
- Budget exhaustion: assert the run stops at the ceiling and compensates fully.

These run in milliseconds against fakes and cover the properties the system is graded on
(N3, N6).
