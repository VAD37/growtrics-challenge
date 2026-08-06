# Mock generation fixtures

Canned output for `mock.py`. Three real chemistry lessons, rendered by the engine repository
(`../growtrics-llm-engine`) and copied here as plain binaries. **Nothing in this repository
generates them**, and nothing in this repository can regenerate them: there is no renderer, no
voice, and no agent on this side.

They are committed rather than downloaded or built so that a clean checkout returns a video that
actually plays. A mock serving a placeholder proves the pipeline moves bytes; it does not prove
the bytes reach a browser intact, which is the last step of the demo.

## The lessons

One brief picks one lesson (`video_for_brief`), and that lesson's four files travel together.

| Slug | Topic | Runs | Video |
|------|-------|------|-------|
| `covalent_bonds` | Why do atoms form covalent bonds? | 81.2 s | 2.3 MB |
| `ionic_vs_covalent` | What is the difference between ionic and covalent bonding? | 88.6 s | 2.9 MB |
| `ph_scale` | How does the pH scale work? | 65.6 s | 2.1 MB |

Each slug has four files:

| File | Serves as | Where it came from |
|------|-----------|--------------------|
| `<slug>.mp4` | `PRIMARY` | the run's `final.mp4`, byte for byte |
| `<slug>.png` | `POSTER` | one frame cut out of that video here, because the run emitted none |
| `<slug>.txt` | `TRANSCRIPT` | the run's `final.srt` with indices and timecodes dropped |
| `<slug>.srt` | nothing | the run's `final.srt`, kept unedited so the `.txt` above can be checked against it |

Plus `<slug>.result.json`, and `sample_lesson.mp4`, both below.

## `<slug>.result.json`

The engine's own account of a run, in the shape `../growtrics-llm-engine/docs/02-result.md`
specifies. **Fabricated.** Those runs predate that contract and emitted no such file, so this one
was written here against these bytes.

What is measured off the committed files: `bytes`, `sha256`, `duration_s`, `width`, `height`,
`fps`, `has_audio`, and the `duration_within_bounds` finding, whose bounds come from that topic's
`examples/<run>/context.json` in the engine repository. What is not: `elapsed_s` is `0.0` because
nobody timed these runs, and `review.score` is `null` because nothing scored them.

Nothing in the pipeline reads it. `mock.py` builds its own worker document, in the ACL's shape,
which is a different shape (`plan/15-engine-seam.md` is where the two meet). It is here so the
seam has a worked example on this side of it, and so a test can hold the document and the bytes
together — `<slug>.result.json` claiming a size these files do not have is the drift worth
catching.

## Known gap

These videos are 854x480 at 15 fps. The `video.short.v1` contract lists
`resolution_at_least_720p`, which `custody/verifier.py` records as **skipped** because the media
probe behind it is not built. When that check lands, these fixtures fail it. That is a fact about
the fixtures, not a reason to loosen the check.

## `sample_lesson.mp4`

The odd one out. A 16 KB colour field with a sine tone, built by `make_sample.sh`, never served
by the mock backend. It stays because a unit test that needs nothing more than a well-formed
`ftyp` box should not read two megabytes to get one.

None of these files is a chemistry lesson anybody vetted. They are output, kept for their bytes.
