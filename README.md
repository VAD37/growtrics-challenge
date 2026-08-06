# growtrics-challenge

Demo backend: an AI chemistry video request service.

```bash
make up      # build and start db, storage, api, worker
make down    # stop them
```

Then `curl localhost:8000/health`. MinIO console is on `localhost:9001`.

Submit a job and it runs: `POST /v1/jobs` queues it, the worker claims it, and
`GET /v1/jobs/{job_id}` moves from `QUEUED` to `SUCCEEDED` with an artifact on it. The video is a
committed fixture, not a rendered lesson; everything around it is real. Scope and build order are
[`docs/demo.md`](docs/demo.md).

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

