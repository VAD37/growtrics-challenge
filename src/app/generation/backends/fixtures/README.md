# Mock generation fixtures

Canned output for `mock.py`. These are real rendered chemistry lessons, produced elsewhere and
committed here as plain binaries. **Nothing in this repository generates them**, and nothing in
this repository can regenerate them: there is no renderer, no voice, and no agent on this side.

They are committed rather than downloaded or built so that a clean checkout returns a video that
actually plays. A mock serving a placeholder proves the pipeline moves bytes; it does not prove
the bytes reach a browser intact, which is the last step of the demo.

| File | Size | Serves as |
|------|------|-----------|
| `lesson_a.mp4` | 534 KB | `PRIMARY`, one of the two videos the brief hash picks between |
| `lesson_b.mp4` | 1.0 MB | `PRIMARY`, the other |
| `poster.png` | 112 KB | `POSTER`, the same one for both |
| `transcript.txt` | 1.1 KB | `TRANSCRIPT`, the same one for both |
| `sample_lesson.mp4` | 16 KB | nothing the backend serves; see below |

`lesson_a` runs 64.6 seconds and `lesson_b` runs 82.3 seconds. Only the video varies per brief:
the poster and transcript belong to `lesson_b` and are served whichever video comes back, which
is a seam a real backend would not have and a fake does not need to hide.

`sample_lesson.mp4` is the odd one out. It is a 16 KB colour field with a sine tone, built by
`make_sample.sh`, and the mock backend never serves it. It stays because a unit test that needs
nothing more than a well-formed `ftyp` box should not read a megabyte to get one.

None of these files is a chemistry lesson anybody vetted. They are output, kept for their bytes.
