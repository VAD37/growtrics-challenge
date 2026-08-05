# Generation worker

Designed last, on purpose. Everything above works against the interfaces on this page with a
scripted fake, so the real implementation can arrive without moving anything else.

**The agent system is its own repository.** The backend finds or spawns a worker and hands it
information. What the agent does inside, which models it calls, which libraries it renders
with, and what intermediate files it produces are not the backend's business. Two things are:
the shape of what goes in, and the validation of what comes back. Everything on this page is
one of those two.

## Shape

```
 backend                    broker                   worker machine
   │                          │                            │
   │ acquire(requirements) ──▶│  pick a placement          │
   │◀── WorkerLease ──────────│  (local | sandbox | cloud) │
   │                                                       │
   │ open_session(RenderRequest) ─────────────────────────▶│ clone template @commit
   │                                                       │ write BriefBundle into workspace
   │                                                       │ run agent skills loop
   │ poll(session) ───────────────────────────────────────▶│   generate -> check -> repeat
   │◀── RenderProgress ────────────────────────────────────│
   │ list_outputs(session) ───────────────────────────────▶│ out/manifest.json
   │ fetch(session, descriptor) ──────────────────────────▶│ stream bytes
   │ close(session, reason) ──────────────────────────────▶│ destroy workspace, self-terminate
```

Pull only. Nothing on this diagram points from the worker into the backend.

## Ports

```python
class WorkerBroker(Protocol):
    async def acquire(self, req: PlacementRequest) -> WorkerLease: ...
    async def renew(self, lease: WorkerLease) -> WorkerLease: ...
    async def release(self, lease: WorkerLease, reason: ReleaseReason) -> None: ...

class GenerationBackend(Protocol):
    async def open_session(self, lease: WorkerLease, req: RenderRequest) -> RenderSessionRef: ...
    async def poll(self, ref: RenderSessionRef) -> RenderProgress: ...
    async def list_outputs(self, ref: RenderSessionRef) -> RenderOutcome: ...
    async def fetch(self, ref: RenderSessionRef, d: ArtifactDescriptor) -> AsyncIterator[bytes]: ...
    async def close(self, ref: RenderSessionRef, reason: CloseReason) -> None: ...

class TemplateSource(Protocol):
    async def resolve(self, ref: TemplateRef) -> ResolvedTemplate: ...   # url + pinned commit

class ResultValidator(Protocol):
    version: str
    async def validate(self, ctx: ValidationContext,
                       candidates: list[ArtifactCandidate]) -> ValidationReport: ...
```

`ResultValidator` is the one contract that matters across this seam, so it is a port with its
own version rather than checks scattered through custody. Upgrading validation is then a swap:
a new implementation, run against the same stored candidates, compared against the old one's
verdicts before it takes over. The version is recorded on every artifact, so "which rules
passed this file" is answerable a month later.

The demo implementation is a chain: path allowlist, size and count caps, structural probe,
output contract match, then the worker's declared checks re-run on our bytes. Each link
returns a verdict rather than raising, so a report lists everything wrong instead of the first
thing wrong.

`PlacementRequest` carries the requirements that decide where a job can run: expected wall
clock, CPU and memory, whether a GPU is needed, egress policy, and a data-residency hint.
Placement is a policy decision, not a hardcoded provider.

| Backend | Status | Use |
|---------|--------|-----|
| `ScriptedBackend` | build first | Returns a committed fixture video and a scripted progress sequence. Every test above the ACL runs on this. |
| `LocalProcessBackend` | demo | Subprocess in a temp directory on the same host. Real files, real streaming, no isolation. |
| `SandboxBackend` | `@TODO` | Container with the filesystem jail and egress allowlist from `06-trust-boundary.md`. |
| `CloudBackend` | `@TODO` | Ephemeral VM. Adds provisioning latency and real placement failure modes. |

The fake is not a lesser implementation. It runs the same contract test suite as the others,
including the failure cases: a session that never finishes, a manifest declaring a file that
does not exist, a stream that exceeds its declared size.

## Template repo contract

The template repo is where the agent's own instructions live. Ours, versioned, pinned by
commit. The backend supplies data; the repo supplies behaviour.

What the backend guarantees to place in the workspace:

```
workspace/
  BRIEF.md              topic, fenced as data
  CONTEXT.md            learner context, one labelled section per item kind
  CONSTRAINTS.md        duration, language, reading level, style
  OUTPUT_CONTRACT.json  required roles, mime types, size caps, named checks
  .trace                trace_id, session_id
```

What the repo must produce:

```
out/
  video.mp4
  manifest.json
  transcript.txt        optional
  poster.png            optional
  logs/agent.jsonl      optional, carries the trace id
```

```json
{
  "contract_version": "1",
  "session_id": "rs_01JB2K...",
  "status": "QUALIFIED",
  "artifacts": [
    {"path": "out/video.mp4", "role": "PRIMARY", "mime": "video/mp4",
     "sha256": "...", "size_bytes": 4821330, "duration_s": 74.2}
  ],
  "checks": [
    {"name": "duration_within_bounds", "passed": true},
    {"name": "audio_track_present", "passed": true},
    {"name": "no_blank_frames", "passed": true}
  ],
  "usage": {"model_calls": 14, "tokens": 40311, "wall_clock_s": 233}
}
```

Exit protocol: `status` is `QUALIFIED`, `EXHAUSTED` (iterations spent without passing its own
checks), or `FAILED`. The backend treats all three as claims. `EXHAUSTED` still gets harvested
and verified, because a video that failed the worker's own bar may still clear ours, and if it
does not, the candidate is useful evidence.

## The in-worker loop

Predesigned skills, committed in the repo:

1. Read the brief and constraints. Produce a scene plan against a closed set of primitives.
2. Render with the chosen library. Manim is the leading candidate for STEM, unconfirmed; see
   `../open-questions.md`.
3. Run the check scripts: duration bounds, audio present, no blank frames, scene count matches
   the plan, no text overflow.
4. On failure, revise and repeat, up to the iteration ceiling in `ExecutionLimits`.
5. Write `out/` and the manifest. Stop.

The checks are scripts in the repo rather than model judgement, so the loop's exit condition
is deterministic. That is also what makes the backend able to re-run them.

`@audit` the iteration loop is where cost concentrates: every revision is another model call
and another render. The ceiling belongs to the backend, not the repo, and the backend enforces
it by session timeout regardless of what the repo does.

## Open questions specific to this page

Which rendering library, whether the agent writes the scene code or fills a template, how
narration and visuals are timed together, and how much the repo can be shared across subjects.
All parked in `../open-questions.md`. None of them change anything above this line, which is
the point of designing the interface first.
