"""Columns in, records out. The only place that knows what order a row arrives in.

Every statement in this package selects a column list from here and every row it reads comes
back through a function here, so "the tuple is in this order" is written down once instead of
being implied by eighteen `row[11]`s spread over two modules. A column added to `schema.sql`
therefore costs one edit, and a column read in the wrong position is a test failure in the
contract suite rather than a status field holding a profile name.

No SQL statement lives here, only the column lists the statements interpolate. That is what
keeps `tests/unit/storage/sql_discipline_test.py` able to say which function executes what: a
statement assembled out of a constant is still written inside the method that runs it.

Three columns are jsonb and each of them is a record rather than a blob, so the mapping runs
both ways in this file:

* `jobs.constraints` and `briefs.constraints` are `JobConstraints`.
* `jobs.failure` is `FailureRecord`, whose `occurred_at` is a datetime and therefore an ISO
  string on the way in. jsonb has no timestamp type, and a float epoch would lose the zone.
* `requests.raw`, `briefs.guard_verdict` and `artifacts.probe` are mappings we deliberately do
  not type (`domain/records.py`), and they pass through untouched in both directions.

`jobs.budget` has no field on `JobRecord`. It is written empty and never read, exactly as
`docs/demo.md` says it should be: the column stays so that restoring cost metering is a feature
rather than a migration on a live table.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final

from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from app.domain.access import Principal
from app.domain.enums import (
    ArtifactRole,
    Audience,
    ContextKind,
    JobStatus,
    ProfileId,
    ReadingLevel,
    ScanVerdict,
    StageName,
)
from app.domain.errors import ErrorCode
from app.domain.records import (
    ArtifactRecord,
    BriefRecord,
    ClaimedWorkItem,
    ContextItem,
    FailureRecord,
    JobConstraints,
    JobRecord,
    StoredRequest,
)

__all__ = [
    "ARTIFACT_COLUMNS",
    "BRIEF_COLUMNS",
    "CLAIMED_COLUMNS",
    "JOB_COLUMNS",
    "PRINCIPAL_COLUMNS",
    "REQUEST_COLUMNS",
    "artifact_from_row",
    "artifact_params",
    "brief_from_row",
    "brief_params",
    "claimed_from_row",
    "constraints_from_json",
    "constraints_json",
    "failure_json",
    "job_from_row",
    "job_params",
    "principal_from_row",
    "request_from_row",
    "request_params",
]

type Params = dict[str, Any]

# --------------------------------------------------------------------------- column lists

PRINCIPAL_COLUMNS: Final[str] = "principal_id, external_id, created_at"

REQUEST_COLUMNS: Final[str] = "request_key, principal_id, raw, received_at"

BRIEF_COLUMNS: Final[str] = (
    "brief_id, job_id, brief_hash, template_version, subject, concept_id, instruction, "
    "context_items, constraints, guard_verdict, sealed_at"
)

JOB_COLUMNS: Final[str] = (
    "job_id, request_key, principal_id, chat_context_id, status, stage, attempt, "
    "progress_percent, profile, output_contract, constraints, failure, artifact_id, brief_id, "
    "version, created_at, updated_at"
)
"""`JobRecord`'s field order, not `schema.sql`'s.

The record is what a repository returns and the column list is only ever used to build one, so
matching the dataclass costs one reader a glance at the DDL and saves every mapping function
below a lookup table. `jobs.budget` is absent for the reason in the module docstring.
"""

CLAIMED_COLUMNS: Final[str] = "item_id, job_id, claimed_by, claimed_until, claim_count"
"""`ClaimedWorkItem`: the five columns a held row answers for, and none of the rest.

A claim hands the runner an identity and a deadline. `available_at` and `run_id` are the queue's
own bookkeeping, and a worker that could read them would be a worker that could reason about its
place in the queue.
"""

ARTIFACT_COLUMNS: Final[str] = (
    "artifact_id, job_id, principal_id, chat_context_id, role, audience, mime, rel_path, "
    "size_bytes, content_hash, storage_uri, probe, scan_verdict, validator_version, "
    "published_at, created_at"
)

# --------------------------------------------------------------------------- jsonb, both ways


def constraints_json(constraints: JobConstraints) -> Jsonb:
    """`JobConstraints` as the jsonb `jobs.constraints` and `briefs.constraints` hold."""
    return Jsonb(
        {
            "max_duration_s": constraints.max_duration_s,
            "language": constraints.language,
            "reading_level": (
                constraints.reading_level.value if constraints.reading_level is not None else None
            ),
        }
    )


def constraints_from_json(raw: Mapping[str, Any]) -> JobConstraints:
    level = raw["reading_level"]
    return JobConstraints(
        max_duration_s=raw["max_duration_s"],
        language=raw["language"],
        reading_level=ReadingLevel(level) if level is not None else None,
    )


def failure_json(failure: FailureRecord | None) -> Jsonb | None:
    """`FailureRecord` as jsonb, or `None` for a job that has not failed.

    `None` here means "no failure to write", which is not the same as `JobTransition.failure`
    being `None`: that one means "leave the column as it is", and the statement expresses it
    with `COALESCE` rather than with this function.
    """
    if failure is None:
        return None
    return Jsonb(
        {
            "code": failure.code.value,
            "stage": failure.stage.value,
            "message": failure.message,
            "retryable": failure.retryable,
            "occurred_at": failure.occurred_at.isoformat(),
            "trace_id": failure.trace_id,
        }
    )


def failure_from_json(raw: Mapping[str, Any] | None) -> FailureRecord | None:
    if raw is None:
        return None
    return FailureRecord(
        code=ErrorCode(raw["code"]),
        stage=StageName(raw["stage"]),
        message=raw["message"],
        retryable=raw["retryable"],
        occurred_at=datetime.fromisoformat(raw["occurred_at"]),
        trace_id=raw["trace_id"],
    )


def context_items_json(items: tuple[ContextItem, ...]) -> Jsonb:
    return Jsonb([{"kind": item.kind.value, "text": item.text} for item in items])


def context_items_from_json(raw: list[Mapping[str, Any]]) -> tuple[ContextItem, ...]:
    return tuple(ContextItem(kind=ContextKind(item["kind"]), text=item["text"]) for item in raw)


# --------------------------------------------------------------------------- rows to records


def principal_from_row(row: TupleRow) -> Principal:
    principal_id, external_id, created_at = row
    return Principal(principal_id=principal_id, external_id=external_id, created_at=created_at)


def request_from_row(row: TupleRow) -> StoredRequest:
    request_key, principal_id, raw, received_at = row
    return StoredRequest(
        request_key=request_key,
        principal_id=principal_id,
        raw=raw,
        received_at=received_at,
    )


def brief_from_row(row: TupleRow) -> BriefRecord:
    (
        brief_id,
        job_id,
        brief_hash,
        template_version,
        subject,
        concept_id,
        instruction,
        context_items,
        constraints,
        guard_verdict,
        sealed_at,
    ) = row
    return BriefRecord(
        brief_id=brief_id,
        job_id=job_id,
        brief_hash=brief_hash,
        template_version=template_version,
        subject=subject,
        concept_id=concept_id,
        instruction=instruction,
        context_items=context_items_from_json(context_items),
        constraints=constraints_from_json(constraints),
        guard_verdict=guard_verdict,
        sealed_at=sealed_at,
    )


def job_from_row(row: TupleRow) -> JobRecord:
    (
        job_id,
        request_key,
        principal_id,
        chat_context_id,
        status,
        stage,
        attempt,
        progress_percent,
        profile,
        output_contract,
        constraints,
        failure,
        artifact_id,
        brief_id,
        version,
        created_at,
        updated_at,
    ) = row
    return JobRecord(
        job_id=job_id,
        request_key=request_key,
        principal_id=principal_id,
        chat_context_id=chat_context_id,
        status=JobStatus(status),
        stage=StageName(stage),
        attempt=attempt,
        progress_percent=progress_percent,
        profile=ProfileId(profile),
        contract_version=output_contract,
        constraints=constraints_from_json(constraints),
        failure=failure_from_json(failure),
        artifact_id=artifact_id,
        brief_id=brief_id,
        version=version,
        created_at=created_at,
        updated_at=updated_at,
    )


def claimed_from_row(row: TupleRow) -> ClaimedWorkItem:
    item_id, job_id, claimed_by, claimed_until, claim_count = row
    return ClaimedWorkItem(
        item_id=item_id,
        job_id=job_id,
        claimed_by=claimed_by,
        claimed_until=claimed_until,
        claim_count=claim_count,
    )


def artifact_from_row(row: TupleRow) -> ArtifactRecord:
    (
        artifact_id,
        job_id,
        principal_id,
        chat_context_id,
        role,
        audience,
        mime,
        rel_path,
        size_bytes,
        content_hash,
        storage_uri,
        probe,
        scan_verdict,
        validator_version,
        published_at,
        created_at,
    ) = row
    return ArtifactRecord(
        artifact_id=artifact_id,
        job_id=job_id,
        principal_id=principal_id,
        chat_context_id=chat_context_id,
        role=ArtifactRole(role),
        audience=Audience(audience),
        mime=mime,
        rel_path=rel_path,
        size_bytes=size_bytes,
        content_hash=content_hash,
        storage_uri=storage_uri,
        probe=probe,
        scan_verdict=ScanVerdict(scan_verdict),
        validator_version=validator_version,
        published_at=published_at,
        created_at=created_at,
    )


# --------------------------------------------------------------------------- records to params


def request_params(request: StoredRequest) -> Params:
    return {
        "request_key": request.request_key,
        "principal_id": request.principal_id,
        "raw": Jsonb(dict(request.raw)),
        "received_at": request.received_at,
    }


def brief_params(brief: BriefRecord) -> Params:
    return {
        "brief_id": brief.brief_id,
        "job_id": brief.job_id,
        "brief_hash": brief.brief_hash,
        "template_version": brief.template_version,
        "subject": brief.subject,
        "concept_id": brief.concept_id,
        "instruction": brief.instruction,
        "context_items": context_items_json(brief.context_items),
        "constraints": constraints_json(brief.constraints),
        "guard_verdict": Jsonb(dict(brief.guard_verdict)),
        "sealed_at": brief.sealed_at,
    }


def job_params(job: JobRecord) -> Params:
    """Every column one submit writes, `budget` included and empty.

    `version` is written rather than defaulted, so a record built at version 0 and a row at
    version 0 are the same fact rather than two that happen to agree.
    """
    return {
        "job_id": job.job_id,
        "request_key": job.request_key,
        "principal_id": job.principal_id,
        "chat_context_id": job.chat_context_id,
        "status": job.status.value,
        "stage": job.stage.value,
        "attempt": job.attempt,
        "progress_percent": job.progress_percent,
        "profile": job.profile.value,
        "output_contract": job.contract_version,
        "constraints": constraints_json(job.constraints),
        "failure": failure_json(job.failure),
        "artifact_id": job.artifact_id,
        "brief_id": job.brief_id,
        "budget": Jsonb({}),
        "version": job.version,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def artifact_params(record: ArtifactRecord) -> Params:
    return {
        "artifact_id": record.artifact_id,
        "job_id": record.job_id,
        "principal_id": record.principal_id,
        "chat_context_id": record.chat_context_id,
        "role": record.role.value,
        "audience": record.audience.value,
        "mime": record.mime,
        "rel_path": record.rel_path,
        "size_bytes": record.size_bytes,
        "content_hash": record.content_hash,
        "storage_uri": record.storage_uri,
        "probe": Jsonb(dict(record.probe)),
        "scan_verdict": record.scan_verdict.value,
        "validator_version": record.validator_version,
        "published_at": record.published_at,
        "created_at": record.created_at,
    }
