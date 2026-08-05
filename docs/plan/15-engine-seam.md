# The engine seam

How this backend hands work to the video engine and reads the result back. The engine lives in
its own repository (`../../growtrics-llm-engine`), and `docs/` in that repository is the
contract. This page is our side of it.

Supersedes the five-method `GenerationBackend` port in `09-generation-worker.md` (D101). The
shape that survived is smaller: write a file, start a run, wait, read a directory.

## The whole interaction

```
  backend                                            engine
     │
     │ 1. mkdir <run-dir>, write context.json
     ├──────────────────────────────────────────────▶ reads it
     │
     │                                                does whatever it does
     │                                                (models, renderers, retries:
     │                                                 none of it is our business)
     │
     │ 2. poll for <run-dir>/out/result.json          writes out/
     │    until it appears or the deadline passes
     │◀ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─┤
     │
     │ 3. harvest out/, verify the bytes,
     │    publish or quarantine
     │
     │ 4. destroy <run-dir>
     ▼
```

Four steps, and the interesting one is step 3.

## What we send

One file, `<run-dir>/context.json`. It is written by `intake` from the sealed `LessonBrief`, so
what reaches an engine is derived from the brief and never from the raw request.

```json
{
  "schema_version": "1",
  "run_id": "rs_01JB2K7Q4X8YV3W",
  "topic": "why do atoms form covalent bonds?",
  "notes": "grade 9. we covered ionic bonding last week.",
  "constraints": {
    "target_duration_s": 60, "min_duration_s": 20, "max_duration_s": 90,
    "language": "en", "reading_level": "grade 9",
    "width": 1280, "height": 720, "fps": 30
  }
}
```

The engine's `run_id` field carries our `RenderSessionId`, which is a one-way hash of our
`run_id`. It is the only identity that crosses. No `job_id`, no `principal_id`, no
`chat_context_id`, no idempotency key. D069 stated that as a rule; this schema is what makes it
true, because there is no field to put them in.

The name collision is worth noticing: their `run_id` is our session id, and our `run_id` never
leaves this process. `intake` does that mapping in one place.

`notes` is where typed `ContextItem`s land, flattened to prose by the brief renderer. The engine
has no notion of `LEVEL` versus `PRIOR_TOPIC`, and giving it one would mean two vocabularies to
keep in step for no gain on the generation side.

### What we deliberately do not send

No model, no renderer, no iteration ceiling, no prompt, no step order. Also no required-artifact
list and no check list: the profile fixes what a run produces, and a per-run copy of the profile
is a second definition of done to keep in step. That is the engine's `D020`, and it retires the
`OutputContract`-rendered-into-the-workspace half of D077 (D102). `OutputContract` survives as
the acceptance test on our side, which is the half that was doing the work.

Constraints are clamped by the engine rather than rejected, and every clamp comes back as a
string in `result.degraded`. That is the opposite of D080's reject-do-not-clamp rule and it is
correct here: these values were validated by our own API before they were written, so an
out-of-range value is our bug, not a learner's request.

## What we get back

`<run-dir>/out/`, and only `result.json` is guaranteed.

```json
{
  "schema_version": "1",
  "status": "ok",
  "run_id": "rs_01JB2K7Q4X8YV3W",
  "title": "Why atoms share electrons",
  "subject": "chemistry",
  "artifacts": [
    {"path": "out/video.mp4", "role": "video", "bytes": 4821330, "sha256": "e3b0...",
     "duration_s": 74.24, "width": 1280, "height": 720, "fps": 30.0, "has_audio": true}
  ],
  "review": {"passed": true, "score": 4.2, "findings": [
    {"check": "duration_within_bounds", "passed": true, "blocking": true, "detail": "74.2s"}
  ], "notes": []},
  "degraded": ["poster: thumbnail extraction failed"],
  "failure": null,
  "engine_version": "0.1.0",
  "elapsed_s": 233.4
}
```

| `status` | Means | Job becomes |
|----------|-------|-------------|
| `ok` | Engine believes it succeeded | Verify. Pass publishes, fail quarantines |
| `needs_review` | A video exists and missed the engine's own bar | Verify anyway. Same two outcomes |
| `failed` | No usable video, `failure.code` set | `FAILED` with `GENERATION_FAILED` |

`needs_review` is harvested, not discarded. A file that missed the engine's bar can clear ours,
and if it misses both, it is evidence about why.

`failure.code` is a closed set: `CONTEXT_INVALID`, `SUBJECT_REFUSED`, `SCRIPT_INVALID`,
`VOICE_FAILED`, `RENDER_FAILED`, `REVIEW_FAILED`, `TOOL_MISSING`, `INTERNAL`. Retry policy comes
off the code, which is why it is closed. `SUBJECT_REFUSED` and `SCRIPT_INVALID` are not worth
retrying with the same context; `TOOL_MISSING` and `RENDER_FAILED` are worth retrying somewhere
else. `SUBJECT_REFUSED` is a successful run of the contract, and reporting it as a crash is how a
scope gate turns into a cost sink.

## Verification, our side

`custody` runs this after the engine is gone, on the bytes it read. Every pass returns a verdict
rather than raising, so one pass reports everything wrong instead of the first thing wrong.

1. **`result.json` present and parseable.** Absent means the run died: no third reading.
   `schema_version` mismatch, or a `run_id` that is not the one we sent, rejects without reading
   further.
2. **Names.** Every path in `out/` against the closed regex published in the engine's
   `02-result.md`. A non-matching path is dropped and logged with the name escaped; a symlink or
   a `..` segment fails the run outright rather than being dropped.
3. **Size and count,** measured while streaming rather than after. A declared 4 MB file that
   keeps producing bytes is cut off, not stored.
4. **Structure.** Container, codecs, duration, resolution, and a non-empty audio track read from
   the file. `@TODO` ffprobe behind the `MediaProbe` port; a stub reads a sidecar for the demo.
5. **`OutputContract` match.** `video.short.v1` wants exactly one `video/mp4`, inside the
   duration bounds we sent, at or above the geometry we asked for, with audio. Missing means
   `DELIVERABLE_INCOMPLETE`.
6. **Checks re-run.** Every name in `review.findings[]` we recognise, plus the profile's own
   list whether or not the engine ran it. A check we cannot run records `UNKNOWN_CHECK` and
   counts as failed, because a check nobody implemented must not read as a check that passed.

`review` is stored either way. Where the engine's verdict and our measurement disagree, the
measurement wins and the disagreement is recorded. An engine reporting `passed: true` on a check
that fails on our bytes is the most useful signal this seam produces, and it exists only because
both sides run the same measurement (D031, unchanged).

We compute a `content_hash` over decoded frames and audio and store it on the artifact row. It
is ours, not a contract field: `Artifact` in the engine forbids extra keys, so there is nowhere
to report one, and requiring it would push a normalisation recipe into every engine that ever
implements this seam. Repeatability is therefore measured and never required (D103).

## Completion

We poll for `out/result.json` until it appears or the deadline passes.

The engine's `context.json` has an optional `callback` block that would push instead. We leave it
unset for the demo (D104). An inbound endpoint from the generation network is a thing to
authenticate, rate limit, and keep unreachable by anything else, and the atomic write of
`result.json` already gives a three-state read of the directory without asking the engine
anything, which matters because a dead engine cannot answer:

| `out/` holds | Reading |
|--------------|---------|
| nothing, or files but no `result.json` | Still working, or died. The deadline separates them |
| `result.json` | Finished |

`@TODO` the poll interval and the generation deadline are both unpicked, and both need one real
render timed end to end. Until that number exists, the lease ordering in `07-distribution.md` is
written down and unvalidated.

## Placement

Where the run directory lives is the same question as where the engine runs.

| Placement | Run directory | Status |
|-----------|---------------|--------|
| Same process, fake | A temp dir. Nothing executes | The demo. `ScriptedBackend` writes a `result.json` and a committed fixture mp4 |
| Subprocess | A temp dir on the API host | Honest about being a demo, no isolation |
| Container | A bind mount | Gets the filesystem jail and the egress allowlist `06-trust-boundary.md` assumes |
| Cloud VM | A volume | Real placement latency and real placement failures |

Only the first is in `docs/demo.md`. The others are the same code path with a different
`GenerationBackend`, which is the point of the port surviving even as its method list shrank.

`@audit` the demo's containment story is that nothing runs. Every control in
`06-trust-boundary.md` that depends on isolation is unenforced until the container row is built,
and a fixture video is not evidence that any of them work.

## What the demo actually builds

`docs/demo.md` stage D2 builds `generation`: the port plus `ScriptedBackend`, returning a
committed fixture video and a `result.json` shaped exactly as above. Everything on this page
above "Placement" is real in the demo, because verification runs against a real file regardless
of who produced it.

Swapping in the engine is replacing one adapter. No schema change, no contract change, no
migration. That is the claim this seam exists to make true, and the fixture is what lets it be
tested before the engine is finished.
