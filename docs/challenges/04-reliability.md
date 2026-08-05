# Reliability under non-determinism (draft 1)

The brief names this the weakest area of most submissions (N3–N6). The position taken here:
push determinism down to the lowest possible layer, and wrap whatever is left in gates that
can only fail loudly.

## Where non-determinism actually lives

| Stage | Non-deterministic? | Strategy |
|-------|--------------------|----------|
| Query → concept mapping | No, if it is a lookup | Keep it a lookup over a concept registry, not a free LLM call |
| Concept → `LessonPlan` | Yes | Schema-constrained output, pinned model+prompt, seed where supported, cached by `content_key` |
| Plan validation | No | Pure code over the plan |
| Scene → visuals | Depends on chosen renderer | Prefer a deterministic renderer; if a model is used, gate its output |
| Narration → audio | Mostly no | Pinned voice + model version |
| Compose | No | Pure function of its inputs |
| Quality gate | No | Pure code over the artifact |

Only two stages are genuinely non-deterministic, and one of them is a choice we have not
made yet. Recognising that is most of the work.

## Six mechanisms

**1. Constrain the output, do not parse the prose.**
The planner returns a schema-validated object, never free text a downstream step has to
interpret. A schema violation is caught at the boundary and retried with the validation
error fed back, bounded at 3 attempts. Free-text-to-video is where flaky pipelines are born.

**2. Close the visual vocabulary.**
The planner may only reference primitives the renderer already implements. An unknown
primitive is a validation failure, not a render-time surprise. This converts "the model
invented something weird" from a runtime crash into a cheap, catchable, retryable error.

**3. Cache by content key.**
`content_key = sha256(normalised_query, concept_id, pipeline_version, model_versions)`.
Identical input returns the identical artifact rather than a new roll of the dice. Repeated
runs of the same concept are then *trivially* consistent, which is exactly what N3 asks for,
and free, which is what N1 asks for. Bumping any pinned version invalidates the key on
purpose.

**4. Stage checkpointing plus scoped retries.**
Each stage writes its output under `(job_id, stage, attempt)`. A retry resumes from the last
good checkpoint instead of restarting from the query. Retry policy is per stage:

| Stage | Max attempts | Backoff | On exhaustion |
|-------|--------------|---------|---------------|
| planning | 3 | exp, jittered | fallback to stored reference plan for the concept |
| plan_check | — | — | counts as a planning failure, feeds error back into retry |
| visuals | 2 per scene | exp | fallback to the simpler renderer tier |
| narration | 3 | exp | fallback to the alternate TTS provider |
| compose | 2 | linear | terminal `COMPOSE_FAILED` |
| quality_gate | — | — | one full re-render, then terminal |

Retryable vs terminal is decided by the error, not by the caller. Network, rate limit, and
timeout are retryable. Schema violation, unsupported concept, and guardrail rejection are
terminal after their own bounded loop.

**5. A fallback ladder that always ends somewhere watchable.**
Preferred renderer → simpler deterministic renderer → static captioned slides with
narration. The last rung uses no model at all and cannot fail non-deterministically. A
learner either gets a good video or a plain one, never a black rectangle. Which rung was
used is recorded on the job so quality degradation is visible rather than silent.

**6. Reference plans as the floor.**
Each of Q1–Q3 ships with a hand-checked `LessonPlan` committed to the repo. If the planner
fails or its output is rejected three times, the pipeline renders the reference plan. This
guarantees the three required queries produce a correct video every single run, and it also
doubles as the fixture for tests.

## Failure states

Every terminal failure sets `error.code`, `error.stage`, `error.attempts`, and a message
written for a human. Half-finished artifacts are deleted, not left in the store. There is no
path from a failed pipeline to a `SUCCEEDED` job, because the quality gate sits inside the
transition.

## How this gets verified

- **Golden-set repeat run.** Q1–Q3, five runs each, with providers faked at the port. Assert
  every run passes the quality gate, and that a fixed seed yields identical artifact
  checksums.
- **Chaos fixtures.** Fake providers that can be told to return malformed JSON, an unknown
  primitive, a truncated audio file, or a timeout. Assert the pipeline reaches a sensible
  terminal state with the right error code and never a corrupt `SUCCEEDED`.
- **Gate unit tests.** Feed the quality gate a black video, a silent track, and a duration
  mismatch. Each must be rejected.
- **Plan validator tests.** Off-concept plan, over-budget scene count, unknown primitive.

Real-provider runs happen manually and produce the three committed videos (D5). Automated
tests never call a paid API.
