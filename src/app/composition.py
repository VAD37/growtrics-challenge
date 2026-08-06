"""The composition root's one job: build the adapters and hand them to the ports.

This module is the only place in `src/app` that is allowed to know both what a port is and which
class implements it. Everything else names a `Protocol` and is handed an object (D065). That is
why the import-linter contracts list every package as forbidden from `psycopg` and `boto3` and
exempt this one along with the two entrypoints: the rule is not "nobody touches a driver", it is
"exactly one file chooses".

It is shared rather than duplicated because `app/main.py` and `app/worker.py` build the same
system. The API reads jobs and artifacts and the worker runs them, but both talk to the same
Postgres, the same bucket, and the same clock, and two copies of that wiring would be two places
for a demo to be configured differently from the thing it demonstrates.

Nothing here connects. `build_adapters` is pure construction: `SqlEngine` holds a DSN, the object
store holds credentials, and the first network call happens when a caller opens something.
Lifetime belongs to the entrypoint, which is why `open_resources` and `close_resources` are
separate functions rather than a constructor and a destructor -- one goes in a FastAPI lifespan
and the other in the worker's `finally`.

`Adapters` is deliberately a bag of ports rather than a class with behaviour. A composition root
that grows methods is an application layer nobody has named.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from app.access.stub import DemoPrincipalResolver
from app.config import Settings
from app.custody.harvester import Harvester
from app.custody.service import Custody
from app.custody.verifier import ContractResultValidator
from app.domain.brief import BriefBundle
from app.domain.contracts import OutputContract
from app.domain.enums import ProfileId
from app.domain.records import BriefRecord
from app.generation.backends.mock import MockGenerationBackend
from app.generation.ports import GenerationBackend
from app.generation.service import RunGeneration
from app.intake.ports import PermissiveGuard
from app.intake.rendering import bundle_for_record
from app.intake.service import SealBrief
from app.orchestration.engine.runner import JobRunner
from app.orchestration.service import (
    ActiveJobLimit,
    ArtifactService,
    JobService,
    ListArtifacts,
    ListJobs,
    OpenArtifactContent,
    QueryJob,
    SubmitJob,
)
from app.storage.objects.s3 import S3ObjectStore
from app.storage.sql.engine import SqlDatabaseProbe, SqlEngine
from app.storage.sql.queue import SqlWorkQueue
from app.storage.sql.repositories import (
    SqlArtifactRepository,
    SqlBriefRepository,
    SqlJobRepository,
    SqlPrincipalRepository,
    SqlRequestStore,
    SqlUnitOfWork,
)

logger = logging.getLogger(__name__)

__all__ = [
    "Adapters",
    "SystemClock",
    "build_adapters",
    "build_artifact_service",
    "build_custody",
    "build_job_service",
    "build_principal_resolver",
    "build_runner",
    "close_resources",
    "open_resources",
]


class SystemClock:
    """`orchestration.ports.Clock` over the wall clock. UTC, always.

    Aware and never naive: `Cursor.encode` refuses a naive datetime (`domain/records.py`), every
    timestamp in the schema is `timestamptz`, and a clock is exactly where that mistake would
    enter the system. `FrozenClock` in `app/storage/memory/clock.py` is the same shape held still.
    """

    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Adapters:
    """Every adapter both entrypoints need, built once.

    The names are the ports, not the classes, because that is what the rest of the system sees.
    """

    settings: Settings
    clock: SystemClock
    engine: SqlEngine
    objects: S3ObjectStore
    backend: GenerationBackend
    """One per process, and shared by the gateway and the harvester on purpose.

    The mock holds its workspaces in memory: `generate` puts the files there and `fetch` reads
    them back, so the object that generated and the object custody pulls from have to be the same
    one. A real backend reads a directory on a machine we rented and would not care, which is why
    this is a wiring fact rather than a property of the port.
    """

    principals: SqlPrincipalRepository
    requests: SqlRequestStore
    briefs: SqlBriefRepository
    jobs: SqlJobRepository
    artifacts: SqlArtifactRepository
    unit_of_work: SqlUnitOfWork
    queue: SqlWorkQueue
    probe: SqlDatabaseProbe


def build_adapters(settings: Settings) -> Adapters:
    """Construct everything. Connects to nothing.

    One `SqlEngine` for the process, shared by all seven repositories, because the pool it will
    hold is a process resource and a second engine would be a second pool nobody opened.
    """
    clock = SystemClock()
    engine = SqlEngine(dsn=settings.database_url)
    objects = S3ObjectStore(
        endpoint=settings.object_store_endpoint,
        public_endpoint=settings.object_store_public_endpoint,
        access_key=settings.object_store_access_key,
        secret_key=settings.object_store_secret_key,
        bucket=settings.object_store_bucket,
        presign_ttl_seconds=settings.object_store_presign_ttl_seconds,
    )
    return Adapters(
        settings=settings,
        clock=clock,
        engine=engine,
        objects=objects,
        # @TODO the engine in `../growtrics-llm-engine` replaces this line and nothing else
        # (`plan/15-engine-seam.md`, D101, D105, D107).
        backend=MockGenerationBackend(
            delay_min_seconds=settings.mock_delay_min_seconds,
            delay_max_seconds=settings.mock_delay_max_seconds,
        ),
        principals=SqlPrincipalRepository(engine, clock),
        requests=SqlRequestStore(engine),
        briefs=SqlBriefRepository(engine),
        jobs=SqlJobRepository(engine, clock),
        artifacts=SqlArtifactRepository(engine),
        unit_of_work=SqlUnitOfWork(engine),
        queue=SqlWorkQueue(engine, clock),
        probe=SqlDatabaseProbe(engine),
    )


async def open_resources(adapters: Adapters) -> None:
    """Everything with a lifetime, brought up before the process serves or claims.

    The pool first, because a process that cannot reach Postgres has nothing to offer and should
    say so at start-up rather than on the first request.

    Then the bucket. `docker-compose.yml` creates no bucket and no fifth service exists to create
    one, so on a clean checkout the first artifact would land in a store that has nowhere to put
    it (D096). `ensure_bucket` is `head_bucket` and a create on absence, it is idempotent, and it
    remembers, so calling it here costs one round trip and moves the failure from "the job that
    finally produced a video" to "the container that started".
    """
    await adapters.engine.open()
    await adapters.objects.ensure_bucket()
    logger.info("database pool open; object bucket %s ready", adapters.objects.bucket)


async def close_resources(adapters: Adapters) -> None:
    """Give the connections back. Safe to call whether or not `open_resources` got that far."""
    await adapters.engine.close()


# --------------------------------------------------------------------------- the API's side


def build_artifact_service(adapters: Adapters) -> ArtifactService:
    """`api.ports.ArtifactService`: listing, and the bytes.

    Custody is on both sides of this. It is the object that reads artifacts because it is the
    object that wrote them, and the read path being the same class as the write path is what
    `plan/03-module-layout.md` means by "the only read path the API is allowed to use".
    """
    custody = build_custody(adapters)
    return ArtifactService(
        listing=ListArtifacts(custody, max_limit=adapters.settings.page_max_limit),
        content=OpenArtifactContent(custody),
    )


def build_job_service(adapters: Adapters) -> JobService:
    """`api.ports.JobService`: submit, read, list."""
    return JobService(
        submit=SubmitJob(
            uow=adapters.unit_of_work,
            clock=adapters.clock,
            admission=ActiveJobLimit(
                adapters.jobs, max_active=adapters.settings.admission_max_active_jobs
            ),
        ),
        query=QueryJob(adapters.jobs),
        listing=ListJobs(adapters.jobs, max_limit=adapters.settings.page_max_limit),
    )


def build_principal_resolver(adapters: Adapters) -> DemoPrincipalResolver:
    """@audit authenticates nothing. See `app/access/stub.py`."""
    return DemoPrincipalResolver(
        adapters.principals,
        default_principal_id=adapters.settings.default_principal_id,
    )


# --------------------------------------------------------------------------- the worker's side


def build_custody(adapters: Adapters) -> Custody:
    """Harvest, verify, store, serve. The backend is the source of the bytes it harvests.

    `Harvester` is given the generation backend as its `CandidateSource`, which is the whole of
    what "we pull, a worker never pushes" means in wiring terms: the only route from a worker's
    workspace into this system is a `fetch` we call, for a path its manifest declared and the ACL
    accepted.
    """
    return Custody(
        harvester=Harvester(adapters.backend),
        validator=ContractResultValidator(),
        store=adapters.objects,
        artifacts=adapters.artifacts,
        clock=adapters.clock,
    )


def build_runner(adapters: Adapters) -> JobRunner:
    """One job, four stages, one writer of status (D066)."""
    return JobRunner(
        jobs=adapters.jobs,
        requests=adapters.requests,
        briefs=SealBrief(adapters.briefs, guard=PermissiveGuard(), clock=adapters.clock),
        generation=RunGeneration(adapters.backend, render=_render_bundle),
        artifacts=build_custody(adapters),
        clock=adapters.clock,
    )


def _render_bundle(
    record: BriefRecord, contract: OutputContract, profile: ProfileId
) -> BriefBundle:
    """Intake's renderer, adapted to `generation.service.BundleRenderer`.

    A function here rather than an import there: `app.generation` may not reach into
    `app.intake`, so the composition root is what joins them (`plan/03-module-layout.md`).
    """
    return bundle_for_record(record, contract, profile=profile)
