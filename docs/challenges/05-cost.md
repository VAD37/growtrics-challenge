# Cost model (draft 1)

The brief scores cost-efficiency and asks what each artifact costs or would cost in
production (N1, N2). Numbers below are placeholders until the media tech is chosen; the
*structure* of the argument is the part that is settled.

## The core bet

For a 60-second chemistry explainer, the expensive path and the good path are not the same
path. Generative video models are the most costly component available and are also the worst
at drawing a correct pH strip or a shared electron pair. Programmatic rendering of a small,
hand-built primitive library is cheaper by orders of magnitude *and* more accurate, because
a diagram of a covalent bond is a drawing problem, not an imagination problem.

So the intended split: the LLM writes the lesson (cents), deterministic code draws it
(fractions of a cent), TTS speaks it (cents). Recorded as a hypothesis in
`open-questions.md`, not yet a decision.

## Per-artifact cost skeleton

One 60s video, six scenes. Fill in once providers are chosen.

| Stage | Unit | Est. qty | Unit cost | Cost | Notes |
|-------|------|----------|-----------|------|-------|
| Concept planning (LLM) | tokens | ~1.5k in / ~1.5k out | TBD | TBD | The only token spend |
| Plan validation | CPU | ms | ~0 | ~0 | Pure code |
| Visual render | CPU-seconds or API calls | 6 scenes | TBD | TBD | Dominates if a media model is used |
| Narration (TTS) | characters | ~900 | TBD | TBD | ~150 words/min |
| Compose | CPU-seconds | ~10 | ~0 | ~0 | ffmpeg-class work |
| Quality gate | CPU-seconds | ~2 | ~0 | ~0 | Probe, not re-encode |
| Storage | GB-month | ~5 MB | TBD | TBD | Negligible at demo scale |
| **Total** | | | | **TBD** | |

The same table gets emitted per job as `cost_estimate_usd` plus a per-stage ledger, so the
answer to "what did this artifact cost" is an API response, not a spreadsheet.

## Levers, ranked by effect

1. **Cache on `content_key`.** A repeat of the same query costs zero. For a demo that runs
   the same three queries repeatedly, this is the single largest saving.
2. **Choose a deterministic renderer over a generative one.** Likely a 10–100x difference on
   the dominant line item, with better diagram accuracy as a side effect.
3. **Gate before you spend.** Plan validation is free and runs before render and TTS. A bad
   plan costs a few hundred tokens, not a full render.
4. **Checkpoint per stage.** A retry re-pays for one stage, not the whole pipeline.
5. **Small model for planning.** Lesson planning over three known concepts does not need a
   frontier model. Pin the smallest model that passes the validator consistently.
6. **Right-size the output.** 720p at a modest frame rate is plenty for a diagram-led
   explainer, and it cuts render and storage together.

## Production extrapolation

At 10k videos/month with a 30% cache hit rate, the marginal cost is
`7,000 × (planning + narration + render)`. That expression is what the table above needs to
fill in. The reason the architecture keeps the renderer behind a port is precisely so this
number can be renegotiated later without touching the job service.

## Quality floor, not just a cost floor

Cheapest is only interesting if the video is still good (N2). The quality gate defines the
floor mechanically: correct duration, non-blank video, audible narration, scene count
matching the plan. Below that floor an artifact is not shipped regardless of how cheap it
was to make.
