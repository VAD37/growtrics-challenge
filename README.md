# growtrics-challenge

Demo backend: an AI chemistry video request service.

```bash
make up            # build and start db, storage, api, worker
scripts/demo.sh    # submit a job, poll it, download the video   (scripts/demo.ps1 on Windows)
make down          # stop them
```

`scripts/demo.sh` is the whole walkthrough. It submits a job, polls `GET /v1/jobs/{job_id}` while
the status climbs from `QUEUED INTAKE 10%` through `RUNNING GENERATING 60%` to
`SUCCEEDED DONE 100%`, lists what the job produced, downloads the primary artifact to
`lesson.mp4`, and prints its sha256 next to a plain verdict: whether those bytes are the committed
lesson the backend serves from, or are not. Expect it to take a minute; the mock generator
deliberately pretends to render for 10 to 60 seconds so a job can be caught mid-flight. Nothing
is claimed that the script did not watch happen.

## The narrated version

```bash
python scripts/api_demo.py                        # against http://localhost:8000
python scripts/api_demo.py --out-dir ./demo-out --no-color
```

`scripts/api_demo.py` walks the same six endpoints with the whole conversation on screen: for
every call, the method and path, the headers that carry meaning, the request body, the status,
the milliseconds and the response body. It downloads all three artifacts rather than only the
video, checks each one against the `ETag` the server sent, ends on the same sha256 verdict
against the committed lesson, and then asks for a job that does not exist so the `404` envelope
is in the transcript too. Standard library only, so any Python 3.9 or newer runs it with nothing
installed: no `uv sync`, no `requests`, no `jq`, no `curl`. It imports nothing from `src/app`,
because a client built out of the server's own code proves less than one that never sees it.

Then `curl localhost:8000/health`. MinIO console is on `localhost:9001`.

The video is a committed fixture, not a rendered lesson; everything around it is real. Scope and
build order are [`docs/demo.md`](docs/demo.md).

```bash
uv sync
uv run pytest              # everything, on a checkout with no Docker and no Postgres
uv run pytest -m docker    # the same walkthrough over HTTP, needs `make up`
```

`tests/integration/pipeline_test.py` is the demo as a test: one process, in-memory storage, the
real routers, worker, runner, custody and generation backend. It needs nothing running.
`tests/integration/compose_e2e_test.py` is the same script against the live stack and is skipped
unless you ask for it.

Design docs live in [`docs/`](docs/). Start at [`docs/plan/README.md`](docs/plan/README.md).

## Notes for interviewer

This project is currently not functioning to generate a video.
Backend only handling agent spawning. Agent is an LLM agent can make a video output (in another repo).
Sample videos are generated in different repo

### Architecture

This is what a job should look like:

1. Receive User API calls with context
2. Create a job. Store it in job inbox. User get jobID and can query status from API
3. Backend worker pull latest job in queue from inbox.
4. Some basic LLM prompt guard to verify job is not prompt injection/attack. (optional)
5. Backend spawn/find a worker to run job task
6. A worker clone template github repo. load context + predefined system prompt
7. template should include some basic iteration and test harness to check artifacts output follow standard
8. Backend see report of worker done task. Scan for artifacts and run another LLM validation on videos and script (optional)
9. Backend store artifacts in DB, persistance storage. Update job status to done.
10. User can query GET job artifacts

