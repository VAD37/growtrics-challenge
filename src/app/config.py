"""Settings. One place for every environment-supplied value; nothing else reads `os.environ`."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment configuration, prefixed `APP_`.

    @audit the defaults below are development credentials. A deployment must supply its own;
    nothing here is safe outside compose on a laptop.
    """

    model_config = SettingsConfigDict(env_prefix="APP_", extra="ignore")

    version: str = "0.1.0"

    database_url: str = "postgresql+psycopg://app:app@db:5432/app"

    object_store_endpoint: str = "http://storage:9000"
    object_store_access_key: str = "minioadmin"
    object_store_secret_key: str = "minioadmin"
    object_store_bucket: str = "artifacts"

    # --- admission -----------------------------------------------------------------
    admission_max_active_jobs: int = 3
    """Capacity gate, not a rate limit (D090).

    A principal already holding this many `QUEUED` or `RUNNING` jobs gets
    `429 TOO_MANY_ACTIVE_JOBS`. It answers "can it handle a new job" with one indexed count,
    and it is the only abuse control left standing after cost metering was cut.
    """

    # --- queue and worker ----------------------------------------------------------
    work_lease_seconds: int = 60
    """How long a claim holds a `work_items` row before its `claimed_until` passes.

    @TODO nothing reacts to a lease expiring yet; see `app/storage/sql/queue.py::reclaim`.
    """

    work_claim_poll_seconds: float = 2.0
    work_max_concurrent_jobs: int = 1
    work_max_claims: int = 3
    """Past this many claims an item is failed rather than retried.

    @TODO read by the reclaim path, which is a stub (scope override item 8).
    """

    job_stale_seconds: int = 900
    """How long a `RUNNING` job may sit untouched before an operator should look at it.

    @TODO nothing sweeps on this; it is here so the timeout system has its number written down.
    """

    # --- job event stream ----------------------------------------------------------
    job_stream_tick_seconds: float = 1.0
    """How often an open `GET /v1/jobs/{job_id}/events` re-reads its job.

    One `JobService.get` per open connection per tick, and at this default a thousand
    concurrent streams is a thousand primary-key reads a second whether or not any of those
    jobs moved. @TODO Postgres `LISTEN`/`NOTIFY` on a `jobs` update deletes this setting
    along with the tick it configures; see `app/api/routers/events.py`.
    """

    job_stream_heartbeat_seconds: float = 15.0
    """How long a stream may go silent before it sends a `: ping` comment.

    Under the shortest idle timeout a proxy is likely to be running, so the connection is
    proved alive rather than dropped without either end being told.
    """

    job_stream_max_seconds: float = 900.0
    """Hard lifetime of one streaming connection.

    An unbounded stream is a resource leak with a feature's name on it. At the cap the server
    closes cleanly and tells the client to reconnect, which costs one round trip and loses
    nothing: the client resumes with `Last-Event-ID`. Matches `job_stale_seconds`, so a stream
    outlives every job an operator would still call healthy.
    """

    # --- paging --------------------------------------------------------------------
    page_default_limit: int = 20
    page_max_limit: int = 100

    # --- limits --------------------------------------------------------------------
    artifact_total_max_bytes: int = 83886080
    """80 MiB, the `video.short.v1` total from `plan/14-api-schema.md`."""

    request_max_body_bytes: int = 65536

    # --- identity ------------------------------------------------------------------
    default_principal_id: str = "u_demo"
    """Used when `X-User-Id` is absent.

    @audit there is no authentication. The header names the caller and is never verified, and
    a request without one becomes this principal rather than a `401`. See
    `app/domain/access.py`.
    """


settings = Settings()
