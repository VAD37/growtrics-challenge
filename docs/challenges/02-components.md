# Component responsibilities (predicted, tech TBD)

Purpose of this doc: name every subsystem and pin down **what it does and what contract it
honours**, before anyone picks a library. The media path in particular has several large
unknowns. Writing the responsibility down first means the tech choice later is a swap, not
a redesign.

Legend: **Settled** = the responsibility is clear and unlikely to move.
**Predicted** = best guess at the responsibility, still to be confirmed.
**TBD** = choice deferred, see `open-questions.md`.

---

## 1. API layer — Settled

Owns: HTTP surface, request/response schemas, status codes, OpenAPI docs, error envelope.
Does not own: any decision about jobs or generation.
Tech: FastAPI + Pydantic v2 (mandated by R1).

## 2. Job service — Settled

Owns: the lifecycle state machine, legal transitions, idempotency by `content_key`,
listing/filtering rules, and what an error means for a client.
Does not own: how a video gets made.
Failure mode it must prevent: a job stuck in `RUNNING` forever because a worker died. Needs
a heartbeat/lease timestamp on the record even in draft 1.

## 3. Job store — Settled

Owns: durability and concurrency for job records.
Contract: `JobRepository` protocol.
Draft 1: in-memory dict + async lock. Later: Postgres.
The optimistic-concurrency guard (`version` field, compare-and-set on update) goes in from
the start; retrofitting it after the fact is where race conditions hide.

## 4. Dispatcher / queue — Settled shape, TBD scale

Owns: getting a queued job onto a worker exactly once, with a retry budget.
Draft 1: `asyncio.TaskGroup` inside the FastAPI process, bounded concurrency semaphore.
Later: Redis/RQ, Celery, or an SQS-style broker. The port (`enqueue`, `claim`, `ack`,
`nack`) is written so the swap is mechanical.
Known limitation of draft 1: process restart loses in-flight jobs. Mitigation is the lease
timestamp above plus a startup sweep that returns orphaned `RUNNING` jobs to `QUEUED`.

## 5. Pipeline orchestrator — Settled

Owns: stage order, per-stage retry policy, fallback ladder selection, gate enforcement,
event emission, cost accumulation, and stage-level checkpointing.
This is the piece that turns non-deterministic parts into a predictable whole (N3). It must
itself be fully deterministic: same plan + same fixtures = same control flow.
Contract it enforces on every stage: pure-ish function of `(job_context, stage_input)` →
`stage_output | StageError(retryable: bool, code, message)`.

## 6. Concept planner (LLM) — Predicted

Predicted responsibility: turn a learner query into a `LessonPlan` — an ordered list of
scenes, each with narration text, on-screen elements chosen from a known primitive
vocabulary, and a duration hint. It writes a *specification*, never pixels and never free
prose that a renderer has to interpret loosely.

Why that shape: constraining the LLM to a closed vocabulary of visual primitives is the
single biggest lever on both cost and repeatability. The model does the part it is good at
(pedagogical sequencing, phrasing) and none of the part it is bad at (consistent visual
output).

Predicted output schema sketch:

```
LessonPlan
  concept_id, title, total_duration_s, difficulty
  scenes[]:
    index, narration (str), duration_s
    visual: { primitive: enum, params: {...}, caption: str }
    key_terms[]
```

Tech: TBD. Requires structured/constrained output support. Cost is text tokens only, so
this is the cheap stage.

## 7. Plan validator / guardrail — Predicted

Predicted responsibility: reject a bad plan **before** any money is spent downstream. Three
layers:
1. Schema validity (Pydantic).
2. Structural budget: scene count, total duration, narration words-per-second sanity,
   every `primitive` known to the renderer.
3. Content guardrail: the plan must actually be about the asked concept, and must not
   contain chemistry claims outside an allowlist for the three required concepts.

The third layer is the interesting one and the least certain. Options range from a keyword
and formula check against a per-concept fact card, up to a second LLM acting as judge. Both
are on the table; the fact-card check is cheaper and more deterministic and is the current
favourite.

Placement matters: this gate is cheap and sits immediately before the expensive stages.

## 8. Scene renderer (visuals) — Predicted, biggest unknown

Predicted responsibility: take one `Scene.visual` spec and produce the frames or clip for
it, plus a manifest describing what it produced and how long it runs.

Predicted properties it must have:
- Deterministic for a fixed spec. Same spec in, byte-identical (or at least
  visually-identical) output. This is what makes N3 achievable at all.
- Cheap. This stage dominates cost if done with a generative video model.
- Legible at small size, since it is an educational explainer, not cinema.

Candidate families, unranked, decision deferred:
- Programmatic 2D animation from the primitive vocabulary (vector/canvas drawing driven by
  code). Deterministic, near-zero marginal cost, quality ceiling set by how good the
  primitive library is.
- Templated slide/diagram rendering with simple motion (pan, fade, build-on).
- Image model per scene, then Ken Burns motion. Non-deterministic, moderate cost.
- Generative video model per scene. Highest cost, lowest determinism, weakest at correct
  chemistry diagrams.

Current prediction: the winning cost/quality point for chemistry explainers is programmatic
rendering of a hand-built primitive library, not generative video. Recorded as a hypothesis,
not a decision.

## 9. Narration synthesizer (TTS) — Predicted

Predicted responsibility: narration text → audio track plus per-word or per-segment timings.
Timings are what let the compositor sync visual builds to speech instead of guessing.
Predicted properties: deterministic given (text, voice, model version); cheap per minute;
one voice for the whole catalogue so videos feel like one series.
Tech: TBD. Both a hosted API and a local model are viable; the port hides which.

## 10. Compositor / muxer — Predicted

Predicted responsibility: assemble scene visuals + narration audio + timings into one
playable file with correct duration, resolution, and codec. Also burns captions if captions
are in scope.
Prediction: this is the least uncertain "hard" component; the standard answer is a
frame-sequence-plus-audio mux, and it is deterministic given its inputs.
Tech: TBD but heavily constrained by whatever the renderer emits.

## 11. Quality gate — Predicted

Predicted responsibility: prove the artifact is watchable before the job flips to
`SUCCEEDED` (N4). Checks it should run, cheapest first:
- File exists, non-trivial size, parses as a valid container.
- Duration within tolerance of the plan's `total_duration_s`.
- Video track present, not a constant blank/black frame.
- Audio track present, non-silent, duration matches video within tolerance.
- Scene count in the manifest matches the plan.

Anything failing here is a retryable error at the composition stage, then a fallback, then
a terminal failure with a specific code. A learner never receives an unchecked file.

## 12. Artifact store — Settled

Owns: bytes, metadata, and addressing. Content-addressed by `content_key`.
Stores per artifact: the mp4, the `LessonPlan` that produced it, the render manifest, the
cost breakdown, and the provider/model versions used. That bundle is what makes a result
reproducible and auditable.

## 13. Observability — Settled

Owns: structured logs correlated by `job_id`, one event per stage transition with duration
and attempt number, and a per-job cost ledger.
Minimum useful set for the demo: a `GET /v1/jobs/{id}/events` timeline, plus stage timings
visible in the job payload. This is what makes the async waiting state "clearly handled"
rather than a spinner (R3, N7).

## 14. Subject registry — Predicted

Predicted responsibility: hold everything topic-specific — prompt/template packs, fact
cards, visual primitive sets — keyed by subject and concept. Exists so the answer to "how
would you add physics?" is a directory, not a refactor.
Draft 1 contains exactly one subject (`chemistry`) with three concepts (Q1–Q3).
