"""Postgres adapters. The only source of truth for state, events, costs, and the queue (D048).

Three files hold the ground the rest of this package stands on:

- `schema.sql` is migration 0001: the six tables of docs/demo.md with their amendments applied.
- `migrate.py` applies it. `python -m app.storage.sql.migrate`, run before anything serves.
- `engine.py` turns `settings.database_url` into a connection, and answers `/health`.

The adapters sit on top of them (D106):

- `rows.py` is the column order and the record mapping, in one place so no statement repeats it.
- `repositories.py` is every port except the queue: principals, requests, briefs, jobs,
  artifacts, and the unit of work that writes a submission's three rows in one transaction.
- `queue.py` is `work_items`, claimed with `FOR UPDATE SKIP LOCKED` under a lease.

Nothing here imports a repository, so the migration still runs in a container that never builds
one, and `app/storage/memory/` holds the same behaviour over dictionaries with the contract suite
in `tests/contract/` holding the two to one set of assertions.
"""
