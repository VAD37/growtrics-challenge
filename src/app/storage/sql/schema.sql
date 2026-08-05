-- Migration 0001. The whole schema the demo runs on.
--
-- Base is the frozen DDL of docs/plan/13-mvp.md (D071). Applied on top of it:
--   A1, A2, A3  artifacts.role, artifacts.audience, artifacts.rel_path
--   A4          jobs.profile, with jobs.output_contract holding only the contract version
--   A5, D087    chat_context_id nullable on jobs and on artifacts
--   A6, D088    artifacts.principal_id, and the partial index that makes it worth having
--   D093        five tables keep their frozen DDL and are not created here
-- and the scope override: no idempotency key anywhere, and a `requests` row instead.
--
-- Two halves of one submission. `requests` is written once and never updated; `jobs` is what a
-- status change touches. `jobs.request_key` is the only join between them, and it is also the
-- seed every other id in the row's story is derived from (app/domain/ids.py).
--
-- Column types follow app/domain/records.py field by field. That module is the in-process shape
-- of these rows, and a repository that cannot round-trip a record has a schema bug, not a
-- mapping bug.
--
-- Applied by app/storage/sql/migrate.py in one transaction. Postgres makes DDL transactional,
-- so a failure halfway leaves no table behind and no bookkeeping row either.

-- access ---------------------------------------------------------------

CREATE TABLE principals (
    principal_id      text PRIMARY KEY,
    external_id       text UNIQUE NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- intake ---------------------------------------------------------------

-- The body exactly as it arrived. Nothing branches on `raw`, so it is jsonb and not a set of
-- columns: parsing it into columns would make it no longer verbatim.
CREATE TABLE requests (
    request_key       text PRIMARY KEY,
    principal_id      text NOT NULL REFERENCES principals,
    raw               jsonb NOT NULL,
    received_at       timestamptz NOT NULL DEFAULT now()
);

-- What we asked the generator for, as opposed to what the user typed. Sole writer `intake`
-- (D066). `job_id` carries no foreign key here, as frozen: the brief is sealed inside the run
-- and the demo never reads one back through the job.
CREATE TABLE briefs (
    brief_id          text PRIMARY KEY,
    job_id            text NOT NULL,
    brief_hash        text NOT NULL,
    template_version  text NOT NULL,
    subject           text NOT NULL,
    concept_id        text,
    instruction       text NOT NULL,
    context_items     jsonb NOT NULL DEFAULT '[]',
    constraints       jsonb NOT NULL DEFAULT '{}',
    guard_verdict     jsonb NOT NULL DEFAULT '{}',
    sealed_at         timestamptz NOT NULL DEFAULT now()
);

-- orchestration --------------------------------------------------------

-- `budget`, `attempt`, `version`, `output_contract` and `profile` stay even though the demo
-- writes a constant into each. Adding a column later is a migration nobody coordinates with a
-- client; changing what a column means is the expensive one (docs/demo.md, "Tables").
--
-- `constraints` is here because JobRecord carries the effective constraints and a job document
-- has to answer for them from QUEUED onward, before any brief row exists to read them from.
CREATE TABLE jobs (
    job_id            text PRIMARY KEY,
    request_key       text NOT NULL REFERENCES requests,
    principal_id      text NOT NULL REFERENCES principals,
    chat_context_id   text,
    brief_id          text,
    status            text NOT NULL,
    stage             text NOT NULL,
    attempt           int NOT NULL DEFAULT 0,
    progress_percent  int NOT NULL DEFAULT 0,
    profile           text NOT NULL DEFAULT 'video.short.v1',
    output_contract   text NOT NULL DEFAULT 'v1',
    constraints       jsonb NOT NULL DEFAULT '{}',
    artifact_id       text,
    budget            jsonb NOT NULL DEFAULT '{}',
    failure           jsonb,
    version           int NOT NULL DEFAULT 0,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX jobs_by_principal ON jobs (principal_id, created_at DESC);

-- The admission count of D090 reads this one: three active jobs and the fourth submit is a 429.
CREATE INDEX jobs_active ON jobs (status) WHERE status IN ('QUEUED', 'RUNNING');

CREATE INDEX jobs_by_request_key ON jobs (request_key);

-- The queue. `claimed_by` and `claimed_until` are the lease; `FOR UPDATE SKIP LOCKED` over the
-- index below is the claim. Nothing here references a worker, because workers are ephemeral and
-- a session id is a derived value rather than a row (D072).
CREATE TABLE work_items (
    item_id           text PRIMARY KEY,
    job_id            text NOT NULL REFERENCES jobs,
    run_id            text,
    available_at      timestamptz NOT NULL DEFAULT now(),
    claimed_by        text,
    claimed_until     timestamptz,
    claim_count       int NOT NULL DEFAULT 0,
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX work_items_claimable ON work_items (available_at) WHERE claimed_by IS NULL;

-- custody --------------------------------------------------------------

-- `principal_id` and `chat_context_id` are denormalised at insert (A6/D088, D073) so a learner's
-- listing is one index scan with no join, and so the listing index can exclude rows it must not
-- show rather than trusting a WHERE clause somebody may forget.
CREATE TABLE artifacts (
    artifact_id       text PRIMARY KEY,
    job_id            text NOT NULL REFERENCES jobs,
    principal_id      text NOT NULL REFERENCES principals,
    chat_context_id   text,
    role              text NOT NULL,
    audience          text NOT NULL DEFAULT 'LEARNER',
    mime              text NOT NULL,
    rel_path          text,
    size_bytes        bigint NOT NULL,
    content_hash      text NOT NULL,
    storage_uri       text NOT NULL,
    probe             jsonb NOT NULL DEFAULT '{}',
    scan_verdict      text NOT NULL,
    validator_version text NOT NULL,
    published_at      timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- The two predicates are the access control, not a performance hint. A listing that plans on
-- this index cannot reach a quarantined row or an operator-audience row, so the harvested agent
-- log and the learner's video are separated by the index rather than by the query text.
CREATE INDEX artifacts_by_principal ON artifacts (principal_id, created_at DESC)
    WHERE scan_verdict = 'CLEAN' AND audience = 'LEARNER';

CREATE INDEX artifacts_by_job ON artifacts (job_id);

-- Cross-job reuse is parked as Q-AC. The index exists now because the question is asked of the
-- column, and adding it later means an index build on a live table.
CREATE INDEX artifacts_by_hash ON artifacts (content_hash);

-- @TODO the two frozen by-context indexes are not created: `jobs_by_context` and
-- `artifacts_by_context` (D073). Every row the demo writes has a null chat context, so both
-- would index nothing but still be written on every insert. They come back with the chat
-- context feature, in the same migration that creates `chat_context_grants` (D093).

