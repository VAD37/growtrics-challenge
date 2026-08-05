# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Rules

- always load skills: caveman full mode and avoid-ai-writing when writing docs markdown

## Reference

Read before changing anything. Docs are the source of truth. Only the spine exists in code:
packages, two entrypoints, `/health`, and compose. Build order is `docs/plan/15-demo-cut.md`.

| Doc | Contents |
|-----|----------|
| `docs/decisions.md` | Append-only decision log, one line each. Current state of every choice |
| `docs/open-questions.md` | Deferred choices and what closes each |
| `docs/notes.md` | Input context from review rounds, captured verbatim. Input, not decision |
| `docs/plan/` | Current design. Start at `plan/README.md`. `plan/15-demo-cut.md` is the build order; `plan/13-mvp.md` holds the frozen SQL schema and `/v1` contract it subsets; `plan/12-data-control.md` is the system in one diagram; `plan/14-api-schema.md` holds the edge types and what a deliverable is |
| `docs/challenges/00-requirements.md` | Brief extracted from the PDF, with stable ids (R/Q/N/D) |
| `docs/challenges/01-05` | Round-1 design. Superseded on the generation path by `plan/`, see D023 |

Source brief: `Agentic_Backend_Challenge_AI_Chemistry_Video_Request_Service.pdf`.

## Working rules

- Every decision gets one appended line in `docs/decisions.md`. Never edit or delete an
  existing line; supersede it and cite the old id.
- Design discussion belongs in `docs/`, not here and not in `README.md`.
- Requirement ids (`R1`, `Q2`, `N3`, `D5`) are stable. Cite them instead of restating the brief.
- Media tech (renderer, TTS, agent template repo, LLM provider) is deliberately unchosen. Add
  candidates to `docs/open-questions.md`; do not pick one silently.
- Deferred work is `@TODO` in code. Unproven security assumptions are `@audit`. Neither is
  dropped silently.
- Build only what `docs/plan/15-demo-cut.md` lists (D085). `docs/plan/10-scope-matrix.md` holds
  the wider cut list; do not build a row the reviewer has not picked.
- The SQL schema and the `/v1` contract in `docs/plan/13-mvp.md` are frozen (D071). Amendments
  live in `plan/14-api-schema.md` (A1 to A4) and `plan/15-demo-cut.md` (A5, A6), each with a
  decision line. Everything else can be rewritten behind a port.

## Project settings

- Python 3.14 (`.python-version`), managed by `uv`.
- Lint and format: `ruff`. Line length 100.
- Tests: `pytest` + `pytest-asyncio` (auto mode), rooted at `tests/`.
- Mandated framework once code starts: FastAPI + Pydantic v2 (requirement R1).
- Persistence: Postgres is the only source of truth (D048). Object storage holds artifact
  bytes. Both run in `docker-compose.yml` at the repository root (D094).
- `import-linter` contracts in `pyproject.toml` enforce the dependency rule. Run
  `uv run lint-imports`. @TODO no CI runs them yet.

## Commands

```bash
make up                      # build and start db, storage, api, worker
make down                    # stop them
uv sync                      # create .venv, install deps + dev group
uv run ruff check .          # lint
uv run ruff check --fix .    # lint with autofix
uv run ruff format .         # format
uv run pytest                # all tests
uv run pytest tests/path_test.py::test_name   # single test
uv run lint-imports          # architecture contracts
uv add <pkg>                 # add runtime dep
uv add --dev <pkg>           # add dev dep
```

## Structure

One package per bounded context, per `docs/plan/03-module-layout.md`. Every package below is a
docstring and nothing else so far; `domain/` imports nothing, `api/` imports no adapter.

```
.
├── docker-compose.yml    db, storage, api, worker (D053, D094)
├── Makefile              up, down
├── infra/Dockerfile      one image, two entrypoints
├── .env.example          every value is also a compose default
├── src/app/
│   ├── main.py           API entrypoint + composition root; /health only
│   ├── worker.py         runner + sweeper entrypoint; idles
│   ├── config.py         settings, the only reader of the environment
│   ├── api/              FastAPI routers and schemas, no domain rules
│   ├── domain/           pure types, aggregates, events, failure codes
│   ├── orchestration/    CORE: job use cases + workflow engine + steps
│   ├── intake/           CORE: sanitise, guard, classify, seal the LessonBrief
│   ├── custody/          harvest, verify, store, serve artifacts
│   ├── generation/       ACL over the external agent worker; ports + fakes
│   ├── access/           principal, entitlement, balance (stub)
│   ├── observability/    events, metrics, tracing (all written to SQL)
│   └── storage/          sql/ + objects/ adapters; memory/ test doubles
├── tests/{unit,contract,redteam,integration}/
├── docs/
│   ├── challenges/       round-1 docs + extracted requirements
│   ├── plan/             round-2 design (see Reference)
│   ├── decisions.md      append-only
│   ├── open-questions.md
│   └── notes.md          captured input context
├── pyproject.toml        deps, ruff, pytest, import-linter config
├── .python-version       3.14
└── README.md             intentionally minimal
```
