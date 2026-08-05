# Open questions

Everything not yet decided, with the reason it is still open and what would close it.
Resolved items move to `decisions.md` as a new line.

## Media path (the large deferred components)

**Q-A. Which renderer family for scene visuals?**
Blocks: cost model, compositor choice, primitive library design.
Closes when: a spike renders one scene of Q1 in two candidate families and the outputs are
compared on legibility, determinism, and cost per scene.
Leaning: deterministic programmatic 2D rendering (see D019).

**Q-B. What exactly is a visual primitive?**
The planner writes specs against a closed vocabulary, so the vocabulary is load-bearing for
both quality and constraint. Needs a first cut covering Q1–Q3: pH strip/scale, molecule,
atom with shells, electron pair, ion with charge, arrow/transfer, comparison table, title
card.
Closes when: the three reference lesson plans are written by hand and the union of what they
need becomes the v1 list.

**Q-C. TTS provider, and hosted or local?**
Trade: hosted is better quality and costs per character; local is free per run and
deterministic but heavier to set up.
Closes when: a one-paragraph sample from each is heard side by side.

**Q-D. LLM provider and model size for planning.**
Requirement is structured/constrained output support. Preference is the smallest model that
passes the plan validator consistently across repeated runs.
Closes when: the validator exists and can be run 10x against two candidate models.

**Q-E. Does the compositor need word-level timings, or are per-scene durations enough?**
Word-level enables narration-synced builds and captions; per-scene is far simpler.
Closes when: the renderer family is chosen, since it determines whether builds are even
expressible.

## Pipeline questions

**Q-F. Guardrail: fact-card check or LLM judge?**
Fact card is deterministic and cheap; a judge catches more but adds a non-deterministic step
inside the reliability layer, which is uncomfortable.
Leaning: fact card for draft 1, judge only if the fact card proves too blunt.

**Q-G. How is `LessonPlan` versioned?**
Changing the schema must invalidate `content_key`, otherwise the cache serves artifacts made
under old rules. A `pipeline_version` constant is the obvious answer; confirm it covers plan
schema, prompt text, primitive library, and provider versions together.

**Q-H. Scene-level parallelism?**
Rendering six scenes concurrently is an easy win if the renderer is CPU-bound and pure. Adds
partial-failure handling. Decide after Q-A.

## Operational questions

**Q-I. Orphaned `RUNNING` jobs after a process restart.**
Plan is a lease timestamp plus a startup sweep back to `QUEUED`. Confirm the sweep is safe
against a job that is genuinely still running in another worker, once there is more than one
worker.

**Q-J. Where does the demo run?**
Local only is fine for the brief. If a hosted demo is wanted, artifact URLs and the artifact
store both change shape.

## Deliverable questions

**Q-K. Video length and format target.**
Assumed 45–90s, 720p, mp4/H.264+AAC. Unconfirmed. Affects cost, render time, and how many
scenes a plan may contain.

**Q-L. Captions burned in, sidecar `.vtt`, or none?**
Affects the compositor and whether word timings are needed (Q-E).

## Round 2 (see `plan/`)

**Q-M. Which enterprise prompt guard sits behind `GuardPort`?**
Candidates to compare: Azure AI Content Safety Prompt Shields, AWS Bedrock Guardrails,
Lakera Guard, Nvidia NeMo Guardrails. Trade is detection rate against added latency and a
per-call cost on every submission, including the ones that were never going to be accepted.
Closes when: the rule-based default and the red-team corpus both exist, so a vendor can be
scored against a baseline instead of against nothing.

**Q-N. Which placement backend does the demo actually use?**
Local subprocess is honest about being a demo and has no isolation. A container gets the
filesystem jail and egress allowlist that `06-trust-boundary.md` assumes. Cloud VM adds
provisioning latency and real placement failures.
Closes when: the reviewer picks row 35 in `plan/10-scope-matrix.md`.

**Q-O. One template repo or one per subject?**
A shared repo with subject packs keeps the skills in one place. Per-subject repos let a
physics template evolve without risking chemistry. Pinning is by commit either way.

**Q-P. Does the agent write scene code, or fill a template?**
Writing code is more flexible and much harder to contain. Filling a parameterised template is
the conservative version of D019 and makes the in-worker checks meaningful.
Closes when: one scene of Q1 is built both ways and compared on iteration count and failure
modes.

**Q-Q. Manim confirmed, and what does one video cost in wall clock?**
Render time drives the generation deadline, the worker TTL, and therefore the whole lease
ordering in `07-distribution.md`. A five-minute render changes the polling contract.

**Q-R. Who owns the worker's model credentials, and how is spend attributed back to a job?**
The worker holds its own key by design (`06-trust-boundary.md`), which means spend is reported
by the worker in `usage` and is therefore a claim like everything else it says.
`@audit` an untrusted spend report is a poor basis for billing.

**Q-S. Artifact storage: object store, database blob, or filesystem?**
The notes say "database or whatever storage". Filesystem for the demo. The choice changes
whether `content_url` redirects to a signed URL or streams through the API.

**Q-T. Can a run actually be cancelled mid-generation?**
Closing a session and releasing a lease is straightforward. Stopping an agent that is halfway
through a render, without leaving a partial artifact or a leaked machine, is not. Decides
whether the cancel endpoint is real or best-effort theatre.

**Q-U. Where do personalised memories come from, and are they trusted?**
The notes say context may come from memories personalised to the user. If those memories were
themselves written from user input, they are untrusted and belong in the same fenced data
block as the instruction. If they come from a system we control, they could be trusted.
Closes when: the source of `ContextItem(kind=MEMORY)` is named.

**Q-V. Does the demo need a real generated video, or is a fixture enough?**
Everything above the generation seam is provable with `ScriptedBackend`. A real video proves
the seam itself. It is also the largest single piece of work in the plan.

## Round 2b

**Q-W. Who issues `chat_context_id`, and can it be recycled?**
We index by it and do not own it (D059). If the chat service ever reuses an id, the artifact
history for a conversation silently mixes two conversations. Needs a stated guarantee from
whoever mints them, or we hash it with a tenant salt and stop caring.

**Q-X. Are personalised memories trusted or untrusted?**
Supersedes the framing in Q-U now that context items are typed. If memories are derived from
things the user typed, they are untrusted and belong in the fenced data block with everything
else. If a system we control writes them, they could carry more weight. Decides whether
`ContextItem(kind=MEMORY)` gets its own handling or shares the default path.

**Q-Y. Unit price table: where does it live and how does it version?**
`unit_price_version` is recorded on every cost entry so a historical figure stays
reproducible, but nothing yet says whether prices are a config file, a table, or fetched.
Closes when: the first real model provider is chosen.

**Q-Z. What is the per-context daily ceiling, in actual numbers?**
The mechanism is decided (D057); the values are not. Too low and a legitimate learner is cut
off mid-lesson, too high and it is not a control. Closes when one real generation has been
measured end to end.

**Q-AA. Migrations: Alembic from the start, or `create_all` plus a note?**
`create_all` runs a demo and is not a migration strategy. Alembic costs an hour and makes the
schema reviewable as a diff. Row 47 in `plan/10-scope-matrix.md`.

**Q-AB. Does the worker read from the object store, or does the backend stream bytes to it?**
The plan says the backend pulls from the worker and writes to storage, which keeps the worker
credential-free. Giving the worker a scoped upload token would be faster for large files and
would put a credential inside an untrusted workspace. The current answer is no; it is worth
recording as a question because the performance argument will come back.

## Round 3 (API schema, see `plan/14-api-schema.md`)

**Q-AC. Are artifacts reused across jobs, or only within one?**
D010 content-addresses an artifact by `hash(query, concept, pipeline_version, ...)` so a repeat
query serves the existing file. D055 derives `artifact_id = uuid5(NS_ART, job_id | content_hash)`,
which includes the job and therefore cannot dedupe across jobs. They disagree, and it costs
money: N1 and N3 both want the second learner asking Q2 to get the first learner's video instead
of paying for another render. It is also a privacy question, because reuse means one learner's
bytes are served to another, and a video rendered from personalised context is not safe to share.
Closes when: someone decides whether reuse is keyed on the brief hash (safe, narrow) or on the
concept (cheap, leaky). The `artifacts_by_hash` index exists either way; nothing in the MVP path
depends on the answer.

**Q-AD. Does the HTML deliverable ship, and is there an isolated origin to serve it from?**
`html.lesson.v1` is designed in `plan/14-api-schema.md` and not built. The sanitiser is a day of
work; the isolated origin is a deployment decision nobody has made. Without the origin, the
profile can still ship with `disposition: attachment`, which downloads instead of renders and
loses most of the point.
Closes when: the reviewer says whether an interactive lesson is a product goal at all.

**Q-AE. Can one job have two primaries?**
A profile that produces both a video and an interactive page has no way to express itself today:
`primary_artifact_id` is one field. The alternative is two jobs from one request, which breaks
one-brief-one-job. Not a problem until a profile wants it.

**Q-AF. Does the chat product need `parent_job_id`?**
"Make it shorter" is the obvious next thing a learner types. Today that is a new brief and
therefore a new job with no link back to what it revises. A `parent_job_id` would give the panel
a thread and give cost control a way to see a regeneration loop, which is exactly what D057's
per-context ceiling is defending against. Deliberately not added yet.
