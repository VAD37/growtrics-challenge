# growtrics-challenge

Demo backend: an AI chemistry video request service.

```bash
make up      # build and start db, storage, api, worker
make down    # stop them
```

Then `curl localhost:8000/health`. MinIO console is on `localhost:9001`.

Only the spine exists: packages, entrypoints, and infrastructure. Build order is
`docs/plan/15-demo-cut.md`.

Design docs live in [`docs/`](docs/). Start at [`docs/plan/README.md`](docs/plan/README.md).
