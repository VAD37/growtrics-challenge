# Scope matrix

The cut list. Every subsystem in this plan, what it costs, and a recommendation. Reviewer
picks a column per row; the picks become decision lines in `../decisions.md`.

Rows marked **2b** moved or appeared because of review round 2b. Reasoning in `11-triage.md`.

The reviewer has since cut further than any row here. `../demo.md` is the picked scope
(D085); this table stays as the full menu and as the record of what each row costs.
Rows marked **3** appeared with the API schema draft. Reasoning in `14-api-schema.md`.

Levels:

- **BUILD** real code with tests.
- **STUB** the port exists, one fake adapter, contract test suite, no real implementation.
- **DEFER** interface only, `@TODO` in code, nothing runs.

Cost is rough implementation effort, not runtime cost.

| # | Subsystem | Doc | Cost | Recommend | Why |
|---|-----------|-----|------|-----------|-----|
| 1 | Domain types and aggregates | 01 | S | BUILD | Everything else is typed against these |
| 2 | Job lifecycle and transitions | 01 | S | BUILD | The invariants are the graded part |
| 3 | Workflow engine: steps, checkpoints, resume | 05 | L | BUILD | Declared core. Nothing else demonstrates failure survival |
| 4 | Retry, backoff, classification, compensation | 05 | M | BUILD | Half the engine's value; cheap once the engine exists |
| 5 | Degradation ladder rung 3 (reference plan render) | 05 | M | pick | Turns agent flakiness into a quality dip. Cut if the agent is reliable enough |
| 6 | Sweeper: lease expiry, orphan recovery | 05, 07 | S | BUILD | Small, and it is the only proof that a crashed run recovers |
| 7 | Intake sanitiser and `SanitisedText` | 06 | S | BUILD | Type-level guarantee, tiny to write |
| 8 | Guard rule set plus red-team corpus | 06 | M | BUILD | The stated core. Cheapest security story with real evidence |
| 9 | Enterprise prompt guard adapter | 06 | S | DEFER | Port exists, no vendor chosen |
| 10 | Intent classifier and concept registry | 06 | S | BUILD | Deny by default, static lookup, no model |
| 11 | Brief renderer and fence escaping | 06 | S | BUILD | The intermediary rule rests on this one function |
| 12 | REST API: submit, list, get, events | 04 | M | BUILD | The deliverable a reviewer touches first |
| 13 | Idempotency key handling | 04 | S | BUILD | At-least-once delivery makes it necessary, and it is 30 lines |
| 14 | Cancel endpoint | 04 | S | pick | Teardown path exists anyway. Endpoint is the extra |
| 15 | Event feed with cursor | 04, 08 | S | BUILD | The user-observation requirement, directly |
| 16 | Progress model, monotonic percent | 08 | S | BUILD | Cheap, and it is what makes the wait tolerable |
| 17 | Metrics port with a database sink | 08 | S | **BUILD** 2b | Events and metrics both read from SQL, which is the source of truth |
| 18 | OpenTelemetry export | 08 | M | DEFER | Same port, later |
| 19 | Structured logs with trace id and redaction | 08 | S | BUILD | Redaction is a security property, not a nicety |
| 20 | In-memory repositories | 03 | S | **test doubles only** 2b | Superseded by SQL; kept honest by the contract suite |
| 21 | SQL repositories, Postgres | 03 | M | **BUILD** 2b | SQL is the only source of truth |
| 22 | Transactional outbox and relay | 07 | M | **BUILD** 2b | Nearly free once a database exists, and it is what keeps events honest |
| 23 | SQL work queue, `FOR UPDATE SKIP LOCKED` | 07 | S | **BUILD** 2b | At-least-once, leases, and dead-letter counting with no broker |
| 24 | Admission control, per-principal concurrency | 07 | S | pick | Matters when a run holds a machine for minutes |
| 25 | Lease fencing tokens | 07 | S | DEFER | Unobservable in one process. Contract test now, adapter later |
| 26 | Artifact custody: harvest, verify, store | 02, 06 | M | BUILD | The only thing standing between an agent and a learner |
| 27 | Path allowlist and size caps on harvest | 06 | S | BUILD | Trivial, blocks the whole traversal family |
| 28 | Real media probe (ffprobe) | 06 | S | pick | Without it, verification checks metadata instead of pixels |
| 29 | Safety scanner adapter | 06 | M | DEFER | Port exists, allowlist and caps cover the demo |
| 30 | Artifact serving from our storage | 04 | S | BUILD | The isolation invariant made real |
| 31 | Access stub: principal, entitlement, balance | 03 | S | STUB | Requested as a stub |
| 32 | Real auth | 03 | M | DEFER | Out of scope for the brief |
| 33 | Generation ports and ACL | 09 | M | BUILD | The seam the whole design is organised around |
| 34 | `ScriptedBackend` fake | 09 | S | BUILD | Every test above the ACL depends on it |
| 35 | `LocalProcessBackend` | 09 | M | pick | The difference between a demo that plays a fixture and one that makes a video |
| 36 | Template repo with agent skills | 09 | L | pick | The largest single piece. Its absence is invisible behind the fake |
| 37 | Sandbox or cloud placement | 09 | L | DEFER | Interface only this round |
| 38 | Contract test suites per port | 03 | M | BUILD | What keeps the fakes honest. Without it the stubbing is decorative |
| 39 | Crash and duplicate-delivery tests | 05 | S | BUILD | Directly demonstrates the survival claim |
| 40 | Step-level cost metering and ceilings | 05 | M | **BUILD** 2b | Abuse prevention at the boundary, not a report afterwards |
| 41 | Docker compose: db, storage, api, worker | 03 | S | **BUILD** 2b | Makes every other claim checkable in one command |
| 42 | Error catalog, no message at a raise site | 04 | S | **BUILD** 2b | One place to audit for leaks, one place to translate |
| 43 | Brief template registry, versioned | 06 | S | **BUILD** 2b | Prompt quality becomes a reviewable diff |
| 44 | Chat context indexing and authorisation | 01, 04 | S | **BUILD** 2b | The frontend is a chat product; the artifact panel needs it |
| 45 | Import-linter contract in CI | 03 | S | **BUILD** 2b | An architectural rule that is not checked is a comment |
| 46 | `ResultValidator` port, versioned | 09 | S | **BUILD** 2b | The only contract that matters across the agent seam |
| 47 | Database migrations | 03 | S | pick 2b | `create_all` runs the demo; it is not a migration strategy |
| 48 | Profile registry and `OutputContract` as request-and-test | 14 | S | **BUILD** 3 | The worker's instruction and the acceptance test become one document that cannot drift |
| 49 | Deliverable projection and `/v1/jobs/{id}/deliverable` | 04, 14 | S | pick 3 | The job document already carries the primary; this is the full set for a lesson panel |
| 50 | `html.lesson.v1`: sanitiser, CSP, isolated origin | 14 | L | DEFER 3 | Agent-authored HTML is active content; the origin is a deployment decision, not code |
| 51 | `ETag` / `If-None-Match` on the job document | 14 | S | pick 3 | Two lines, and polling is the entire observation model |

Cost key: S under an hour, M a few hours, L most of a day or more.

## Recommended minimum slice

Rows marked BUILD, plus 35. That gives a system where a reviewer can `docker compose up`,
submit a request, watch a real event feed, restart the worker mid-job and watch it resume from
its checkpoint, see a malicious request refused with evidence, see a run stopped by its cost
ceiling, and download a video the backend actually verified.

Round 2b grew this. SQL, compose, and cost metering are real work the in-memory plan avoided.
The trade is a system that survives a restart and can be inspected after the fact, instead of
one that forgets everything when the process exits.

The three `pick` rows worth arguing about:

**36, the template repo.** Building it is the difference between demonstrating a backend and
demonstrating a product. Skipping it is defensible because the plan's whole structure exists
to make the generation side replaceable, and the `ScriptedBackend` proves the seam. It is also
the single biggest time sink here.

**5, the reference-plan fallback.** Only worth building if the agent turns out flaky in
practice. Cheap to add later; the ladder already has a rung for it.

**28, the real media probe.** Small, and it changes verification from reading a manifest to
reading a file. If any single `pick` row gets promoted, this is the one.

## What this plan deliberately does not do

Cost analysis, multi-tenancy, real auth, horizontal deployment, dead-letter queues,
data residency, artifact lifecycle and GC, subject packs beyond chemistry. Each is either an
`@TODO` in the relevant doc or a line in `../open-questions.md`. None is necessary for the
claims this design makes.
