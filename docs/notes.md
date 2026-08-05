# Notes

Captured context for design round 2, dated 2026-08-05. This file is input, not a decision.
Decisions derived from it go to `decisions.md`; the design itself goes to `docs/plan/`.

Round 1 (`docs/challenges/`) assumed generation was an in-process pipeline of provider
ports. Round 2 replaces that: generation is an external agent worker service reached through
an interface, and the backend is the thing being designed.

## Framing

- Focus is the backend. Video generation is the last thing designed, not the first.
- Heavy use of mocks and stubs is expected and fine.
- Codebase is Python.
- Anything consciously skipped is marked in code with `@audit` or `@TODO`, not silently
  dropped.

## Video generation, as an interface

Treated as a microservice plus a worker, behind a Python interface.

1. Internal system asks for a worker machine: secure cloud, sandbox, or local machine.
2. Worker clones a template repo. Likely Manim for STEM video.
3. Backend drops the user prompt plus context into markdown files in the workspace.
4. An agent runs inside the worker using predesigned skills and workflows.
5. Those skills contain guards, an iteration loop, and script-driven test checks that repeat
   until a qualified video comes out.
6. Backend crawls the agent server for files and artifacts, and a separate safety and
   quality check runs over what it finds.
7. Files are pushed by the backend, from the agent server into a database or other storage.
8. Artifacts are served to the user directly, with no interaction or connection to the cloud
   agent.
9. The agent LLM must be contained. It must not emit anything outside what we can control.
10. Input is learning material only. The expected output is a video artifact, so that is
    what the backend asks for.

## Backend and API

- REST API modelled as job submission and job query.
- User submits a video generation job with an instruction plus context. Context comes from
  the frontend or from memories personalised to the user.
- User polls for job status.
- User may carry auth, cost, and token balance. All stub or mock for now. No cost analysis
  in this round.
- Metrics are mostly stubs: emit an event, log to console.
- The user must be able to observe their task and its progress.

## Stated core of the system

1. Workflow and pipeline system.
2. Survives failure of any part of the workflow.
3. Survives malicious user prompt input, whether by an enterprise prompt guard or by many
   test-case checks inside the workflow.
4. A clear definition of what the output is.
5. All data passes through an intermediary. The user prompt is wrapped in our own context and
   prompt design, or we only drop a file of instructions to the LLM agent and never use a
   prompt role for user text.
6. Metrics and observation on every part of the system.

## Design method requested

Domain-driven design. Emphasis on the data passed between modules and systems, and on
distributed system design. Drafts stay in `docs/plan/` for review; the reviewer then picks
what ships in the demo and what is deferred.

---

# Review round 2b

Second pass of input, after the first `plan/` draft. Triage of these against the plan is in
`plan/11-triage.md`.

## What the user sees

- A chat window, with panels showing artifacts for each chat context, and a list of older
  artifacts.
- A job status window.
- The chat window also carries redirects to artifact results.

## What the frontend is expected to send

- User id and chat context.
- The instruction written by the user, which is the prompt input to the backend's video
  service.
- Extra context supplied by the frontend, meaning personalised information.

## What the backend does with it

- Separate the user prompt from the job prompt.
- The backend builds its own job prompt, a plan, from the API query and the context it
  received. This intermediary product allows templating of the input and better quality
  control.
- The user's query is one input to that, not the thing passed through.

## Components the backend must have

- SQL as the only source of truth.
- A long-term storage system.
- A docker compose setup.
- Metric events, stored in the database. Nothing fancy.
- Observation reads from the database as its source of truth.
- Cost analysis, with LLM token cost computed inside each workflow step, to prevent abuse.
- Clear separation of data from logic. Data is passed around; the system is built around the
  data schema.
- A clear errors file rather than bare Python strings.
- Validation on the input of every workflow step. Do not trust prompt text or a malicious
  JSON body.
- Strict schema format for JSON and for jobs.
- Every job derives from an idempotency key, which is the main key for the whole system.
- The agent LLM system is its own repository. The backend only finds or spawns an agent and
  passes information. What the agent does and what result types it produces are not the
  backend's concern. The backend does validate the results that come back, and how that
  validation works is an interface that will be upgraded later.
- The core of the backend is modules. Each part of the workflow pipeline is its own atomic
  modular system, sharing only types and data.

# Review round 3b: the demo is smaller than the MVP

Input, not a decision. Triaged into `plan/15-demo-cut.md`.

The MVP in `plan/13-mvp.md` is still too big for the demo. What the demo actually is:

1. A user provides a query: user id, prompt, context.
2. The server returns a job id, if it can handle a new job.
3. The user polls job status until it says done, with related information such as whether
   artifacts exist.
4. The user queries artifacts.
5. The user gets a video.

Anything observational and any metric system, including user cost and budget attached to user
information, is ignored for now. The user being able to call the API for all job statuses and
for artifacts is enough.
