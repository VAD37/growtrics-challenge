# Requirements

Source: `Agentic_Backend_Challenge_AI_Chemistry_Video_Request_Service.pdf` (5 pages).
Extracted verbatim in meaning, restructured into IDs so later docs can reference them.

## Scenario

A learner requests a short educational video explaining a chemistry concept. The backend
accepts the request, processes it as a video-generation job, and lets the client poll until
the video is ready. Latency is explicitly not a concern; the waiting state must be modelled
and exposed through the API.

Backend only. No frontend. Demo client is curl / Postman / script / OpenAPI docs.

## Functional requirements

| ID | Requirement |
|----|-------------|
| R1 | FastAPI backend |
| R2 | Endpoint to request a chemistry concept explanation video |
| R3 | Asynchronous video-generation flow |
| R4 | Endpoint to list requested videos / jobs |
| R5 | Visible status per job |
| R6 | Endpoint to retrieve or open the completed video artifact |
| R7 | Artifact has both visual content and audio, feeling like a normal short educational video |
| R8 | Clear backend boundary between job state, generation logic, persistence, and artifacts |
| R9 | Explanation must be coherent, useful, and visibly tied to the learner query |

## Required queries (full scope)

| ID | Learner query |
|----|---------------|
| Q1 | How does the pH scale work? |
| Q2 | Why do atoms form covalent bonds? |
| Q3 | What is the difference between ionic and covalent bonding? |

No other subjects or chemistry topics required. The design must still make it obvious how
other STEM topics would be added.

## Non-functional requirements

| ID | Requirement |
|----|-------------|
| N1 | Cost efficiency is a scored metric. State what each artifact costs, or would cost in production |
| N2 | Visual quality is a scored metric. Best visual explanation at the cheapest reasonable cost |
| N3 | Reliability under non-determinism: consistent output across repeated runs of the same concept |
| N4 | Validate generated output before it reaches the learner |
| N5 | Understandable failure states, no silent or half-way failures |
| N6 | Retries, fallbacks, guardrails, quality gates where they earn their place |
| N7 | API clarity, job-state handling, error handling, observability |
| N8 | The generation boundary must be able to evolve into a production service |

## Explicit permissions (what may be simplified)

- In-memory persistence is acceptable if the boundary is clean.
- Local file/artifact store is acceptable.
- Mocked or partly simulated generation is acceptable if the design shows where real
  AI/video providers plug in.
- Production polish is not expected.

## Constraints

- Time budget: 90–120 minutes of work.
- An agentic coding harness (Claude Code / Codex / Cursor Agent) must be used throughout.
- Screen and face recording during the session.
- Do not maximise surface area. Deliberate scope choices are part of the grade.

## Deliverables

| ID | Deliverable |
|----|-------------|
| D1 | Codebase containing the FastAPI backend |
| D2 | Short `README.md`: setup, run, API, test instructions |
| D3 | Short architecture note: job lifecycle, persistence/artifact boundary, AI/video boundary |
| D4 | Demo video or API walkthrough covering Q1–Q3 |
| D5 | The three best generated videos committed to the repo, each paired with its input query |
| D6 | GitHub link, read access for `praveen.k@growtrics.ai` and `tech@growtrics.ai` |
| D7 | Zip file of the work |
| D8 | Google Drive link to the screen + face recording |

## Evaluation axes (stated by the brief)

1. Product judgement: what to build, fake, simplify, leave out.
2. Architecture and planning: practical architecture, clean API and job lifecycle, artifact
   contents, async job state, cost tradeoffs, repeatable generation, small but coherent.
3. Reliability under non-determinism. Called out as where most submissions are weakest.
4. AI-agent workflow: clear plans, inspecting generated code, coherent steps, verification.
5. Quality: testing, debugging, error handling, observability.
