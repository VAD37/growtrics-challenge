"""What a worker claimed, what we pulled, and what we concluded.

Three records, in the order custody produces them:

    ArtifactDescriptor   a claim, parsed out of an untrusted manifest by `generation.acl`
    HarvestedFile        bytes we pulled, with our own measured size and our own sha256
    VerifiedArtifact     our verdict, our probe, and the validator version that produced them

The split exists because the worker's own numbers are never evidence (`plan/06-trust-boundary`).
A descriptor says "there is a 4 MB mp4 at out/lesson.mp4"; the harvested file says what actually
arrived, and `claim_agreed` records whether those two agreed, which is a signal worth keeping
rather than a discrepancy to smooth over.

`ArtifactRecord` in `domain/records.py` is the fourth and last shape: the row. Custody builds it
from a `VerifiedArtifact` and it is the only one of the four a client ever sees a projection of.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from app.domain.enums import ArtifactRole, Audience, ScanVerdict
from app.domain.errors import DomainError, ErrorCode


@dataclass(frozen=True, slots=True)
class ArtifactDescriptor:
    """One file a worker says it made, after the ACL has accepted its shape.

    Everything here has already been checked against the output contract: the role exists, the
    media type belongs to that role's part, and `rel_path` is under the harvest root with no
    absolute prefix, no `..`, and no component outside the allowlist. It is still a claim about
    a file nobody has read.
    """

    role: ArtifactRole
    audience: Audience
    media_type: str
    rel_path: str
    declared_size_bytes: int
    declared_sha256: str | None


@dataclass(frozen=True, slots=True)
class HarvestedFile:
    """Bytes we pulled, with numbers we measured ourselves.

    `data` is held in memory because the demo's cap is 64 MiB for one file and the object store
    write is a single put. @TODO stream through the store rather than buffering when the cap
    rises or the backend becomes remote (`docs/plan/06-trust-boundary.md`, "Size and shape").
    """

    descriptor: ArtifactDescriptor
    data: bytes
    size_bytes: int
    content_hash: str
    claim_agreed: bool


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """One named check from the contract, run on our side over the bytes we received.

    `skipped` is not a pass. A deferred check records itself as skipped so the verdict stays
    honest about how much was actually proven; a check that both passed and was skipped would
    be a lie the record refuses to hold.
    """

    name: str
    passed: bool
    skipped: bool
    detail: str

    def __post_init__(self) -> None:
        if self.skipped and self.passed:
            raise DomainError(
                ErrorCode.GENERATION_FAILED,
                {"reason": "check_cannot_pass_while_skipped", "check": self.name},
            )


@dataclass(frozen=True, slots=True)
class VerifiedArtifact:
    """Our verdict on one harvested file, with the version that produced it (D064).

    `validator_version` is recorded per artifact rather than per deployment, so upgrading the
    check chain becomes a swap comparable against the old verdicts instead of an edit spread
    through custody.
    """

    harvested: HarvestedFile
    verdict: ScanVerdict
    checks: tuple[CheckOutcome, ...]
    validator_version: str
    probe: Mapping[str, object]
    # @audit `probe` is untyped for the same reason `ArtifactRecord.probe` is: its keys differ
    # per media type and per prober, it lands in jsonb, and nothing branches on it.

    def failed_checks(self) -> tuple[CheckOutcome, ...]:
        return tuple(check for check in self.checks if not check.passed and not check.skipped)

    def skipped_checks(self) -> tuple[CheckOutcome, ...]:
        return tuple(check for check in self.checks if check.skipped)
