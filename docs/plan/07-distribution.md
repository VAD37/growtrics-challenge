# Distribution

The demo runs in one process. The design is written for more than one, and the difference is
confined to adapter choices. Anywhere that is not true is called out below.

## Roles

| Role | Responsibility | Demo | Later |
|------|----------------|------|-------|
| API node | Accept, validate, enqueue, read projections | `api` service in compose | N replicas behind a load balancer |
| Runner | Lease a run, execute steps, write checkpoints | `worker` service in compose | M processes, own scaling |
| Sweeper | Reclaim expired leases, re-enqueue stalled runs, expire sessions | in the `worker` entrypoint, on a timer | Leader-elected or idempotent on every node |
| Generation worker | Run the agent, produce files | Local subprocess | Sandbox or cloud VM, ephemeral |

API and runner are separate services from the start, one image with two entrypoints, because
the scaling pressures are unrelated: submissions are cheap and bursty, runs are minutes long
and hold a machine. Compose makes that split real rather than aspirational, and it means the
"restart the worker mid-job and watch it resume" demonstration is one `docker compose restart`
away.

## Delivery guarantees

**Queue: at-least-once.** Exactly-once is not available and pretending otherwise pushes the
problem somewhere less visible. Every consumer is idempotent instead:

- Work items carry `(job_id, run_id, attempt_group)`. A runner that receives a duplicate finds
  the run already leased and exits without effect.
- Every step's external effects are keyed by an idempotency key derived from
  `(run_id, step_name, attempt_group)`.
- Artifact publish is keyed by content hash, so the same bytes stored twice produce one
  artifact.

**Job state changes and event emission must not diverge.** A status written without its event
means a progress feed that lies. The state change and the event row commit in one database
transaction through an outbox table, and a relay publishes from it. This was deferred while
the plan assumed in-memory state; with SQL as the source of truth it is a table and a small
relay, so it ships.

**The queue is a table, not a broker.** `work_items` claimed with
`SELECT ... FOR UPDATE SKIP LOCKED`, with a visibility timeout and a claim count. That buys
at-least-once delivery, lease reclaim, and dead-letter counting without a second piece of
infrastructure, and the queue commits in the same transaction as the state it guards. A broker
becomes worth its operational cost when throughput demands it, not before.

## Leases

Two independent leases, different owners, different timeouts.

**Run lease.** A runner claims a run by setting `lease_owner` and `lease_expires_at`, and
heartbeats while it works. If the runner dies, the lease expires and the sweeper returns the
run to the queue. Resume starts from the last checkpoint.

**Worker lease.** The `WorkerBroker` hands out a machine with its own TTL. The worker enforces
the TTL itself and self-destructs on expiry, so a backend crash cannot leak a running machine
indefinitely. `teardown` releases it on the happy path; TTL is the backstop.

Lease durations follow one rule: **worker TTL is longer than the run's generation deadline,
and run lease TTL is shorter than both.** That ordering means the backend always notices a
dead runner before the machine disappears underneath it.

`@audit` a reclaimed run whose original runner is alive but partitioned can produce two
runners on one run. Fencing is the answer: the lease carries a monotonically increasing token,
and any write or effect from a stale token is refused. The demo's single process makes this
unobservable, which is exactly why the test for it belongs in the contract suite rather than
waiting for the deployment that needs it.

## Concurrency control

- Aggregates carry a `version`. Writes are compare-and-set. A losing writer re-reads and
  retries rather than clobbering.
- One transaction touches one aggregate. Cross-aggregate consistency comes from events.
- Derived ids mean a duplicate submission collides on a primary key rather than racing a
  read-then-write. See the idempotency spine in `01-domain-model.md`.
- Queue depth is bounded by admission control rather than by memory. A full queue is
  backpressure, surfaced as `429` with `Retry-After`.
- Admission control caps concurrent runs per principal and in total. Generation holds a
  machine for minutes; unbounded acceptance turns a traffic spike into a fleet bill.

## Failure domains

| Domain | Blast radius | Detection | Recovery |
|--------|--------------|-----------|----------|
| One API node | In-flight HTTP requests only | Health check | Client retries, idempotency key makes it safe |
| One runner | Its leased runs | Lease expiry | Sweeper requeues, resume from checkpoint |
| One generation worker | One session | Heartbeat gap or session timeout | Close, re-place, retry the step |
| Placement provider down | All new generation | `place` failures | Fall back to another provider, then `GENERATION_UNAVAILABLE` and the run waits |
| Model provider down | All `generate` steps | Step failures cluster | Retry with backoff and jitter, degrade down the ladder |
| Job store down | Everything | Write failures | Fail fast, `503`, do not accept work that cannot be recorded |
| Artifact store down | Publishing only | `publish` failures | Retry; the run holds at `PUBLISHING` rather than failing immediately |
| Event sink down | Observability only | Emit errors | Never blocks a job. Events buffer, drop with a counter if buffer fills |

The last row is a deliberate asymmetry. Losing observability degrades operations; blocking
jobs on the observability path turns a monitoring outage into a service outage.

## Time and ordering

- One clock port. Every timeout, lease, and backoff reads from it. Tests freeze it.
- Event ordering is per job, by a dense `seq` from the job aggregate. There is no global
  order and nothing needs one.
- No step depends on wall-clock agreement between machines. Deadlines are computed once by
  the backend and passed down as absolute values with the trace, so a worker with a skewed
  clock cannot extend its own budget.

## What is skipped this round

Service discovery, leader election, partitioning by tenant, multi-region, queue durability
guarantees, dead-letter handling, and back-pressure signalling upstream to the frontend. Each
is a line in `10-scope-matrix.md` and an `@TODO` where the code would otherwise imply it
already works.
