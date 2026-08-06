# Three verbs and no more (D113, supersedes D097). `up` and `down` own the stack; `demo` runs the
# walkthrough, because a reviewer who has just run `make up` should not have to read a script path
# out of a doc to see the thing work. The walkthrough itself still lives in scripts/ -- this target
# invokes one, it is not a second copy of it. Tests are `uv run pytest`. The migration runs inside
# the api container before it serves, one line of that service's command in docker-compose.yml.
#
# `demo` calls the interpreter directly rather than through `uv run`. scripts/api_demo.py imports
# nothing outside the standard library on purpose (D112), and going through uv would put `uv sync`
# back between a reviewer and the demo.

PYTHON ?= python
ARGS ?=

.PHONY: up down demo

up:
	docker compose up -d --build

down:
	docker compose down

# make demo
# make demo ARGS="--instruction 'why does salt melt ice on a road'"
# make demo ARGS="--base-url http://localhost:8000 --user-id u_review --out-dir ./demo-out"
# make demo PYTHON=py                              when `python` is not the name on this machine
demo:
	$(PYTHON) scripts/api_demo.py $(ARGS)
