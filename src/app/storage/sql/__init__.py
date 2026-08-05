"""Postgres adapters. The only source of truth for state, events, costs, and the queue (D048).

Three files hold the ground the rest of this package stands on:

- `schema.sql` is migration 0001: the six tables of docs/demo.md with their amendments applied.
- `migrate.py` applies it. `python -m app.storage.sql.migrate`, run before anything serves.
- `engine.py` turns `settings.database_url` into a connection, and answers `/health`.

The repositories are somebody else's file and they import `engine`. Nothing here imports a
repository, so the migration runs in a container that never builds one.
"""
