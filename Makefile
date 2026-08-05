# Two verbs and no more (D097). A build script that grows verbs becomes a second, undocumented
# interface to the system: the walkthrough lives in scripts/demo.sh and scripts/demo.ps1, and
# tests are `uv run pytest`. The migration runs inside the api container before it serves, one
# line of that service's command in docker-compose.yml.

.PHONY: up down

up:
	docker compose up -d --build

down:
	docker compose down
