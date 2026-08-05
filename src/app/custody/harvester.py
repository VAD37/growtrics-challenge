"""Pull the declared candidates, measure them, and refuse the set if anything is off.

The direction is the invariant: we pull, a worker never pushes. There is no inbound endpoint
from the generation network, so nothing here authenticates a caller, because there is no caller.

Everything a manifest said is a claim. `HarvestedFile` carries the size we counted and the hash
we computed, and `claim_agreed` records whether the worker's numbers matched. A disagreement is
not fatal on its own -- the bytes we have are the bytes we will serve either way -- and it is
worth keeping, because a worker whose claims drift from reality is the early signal that
something in the generation path has gone wrong.

Caps are enforced on the way in rather than after: the source is asked for at most one byte past
the part's cap, so a file that keeps producing bytes is cut off instead of stored.
"""

import hashlib
from collections.abc import Mapping
from typing import Final

from app.custody.ports import CandidateSource
from app.domain.artifact import ArtifactDescriptor, HarvestedFile
from app.domain.contracts import OutputContract, part_for_role
from app.domain.errors import DomainError, ErrorCode
from app.generation.acl import accept_descriptors, parse_outcome

_HASH_PREFIX: Final[str] = "sha256:"


def content_hash_of(data: bytes) -> str:
    """`sha256:` plus lowercase hex. The addressing scheme for every stored artifact.

    Content addressing is why the storage backend can change without touching a row, and why
    `artifacts_by_hash` can answer "have we made this before" (Q-AC).
    """
    return _HASH_PREFIX + hashlib.sha256(data).hexdigest()


def _reject(reason: str, **details: str) -> DomainError:
    return DomainError(ErrorCode.GENERATION_FAILED, {"reason": reason, **details})


class Harvester:
    """Turns a worker's manifest into bytes we hold and numbers we measured."""

    def __init__(self, source: CandidateSource) -> None:
        self._source: CandidateSource = source

    async def harvest(
        self,
        payload: Mapping[str, object],
        contract: OutputContract,
        *,
        session_id: str,
    ) -> tuple[HarvestedFile, ...]:
        """Parse, accept, pull, measure.

        The ACL runs first and in full, so a hostile path is refused before the source is ever
        asked for it. That ordering is the difference between an allowlist and a log line.
        """
        outcome = parse_outcome(payload)
        descriptors = accept_descriptors(outcome, contract, expected_session_id=session_id)

        harvested: list[HarvestedFile] = []
        total = 0
        for descriptor in descriptors:
            part = part_for_role(contract, descriptor.role)
            data = await self._source.fetch(
                session_id,
                descriptor.rel_path,
                max_bytes=part.max_bytes,
            )
            if len(data) > part.max_bytes:
                raise _reject(
                    "size_over_cap",
                    rel_path=descriptor.rel_path,
                    cap=str(part.max_bytes),
                )
            if not data:
                raise _reject("empty_candidate", rel_path=descriptor.rel_path)

            total += len(data)
            if total > contract.total_max_bytes:
                raise _reject("total_over_cap", cap=str(contract.total_max_bytes))

            harvested.append(
                HarvestedFile(
                    descriptor=descriptor,
                    data=data,
                    size_bytes=len(data),
                    content_hash=content_hash_of(data),
                    claim_agreed=_claim_agreed(descriptor, data),
                )
            )
        return tuple(harvested)


def _claim_agreed(descriptor: ArtifactDescriptor, data: bytes) -> bool:
    """Did the worker's own numbers match the bytes it produced.

    Recorded, never acted on. Feeding this into the verdict would let a worker fail its own
    output by mistyping a size, and would let a careful one buy trust by getting a number right.
    """
    if descriptor.declared_size_bytes != len(data):
        return False
    if descriptor.declared_sha256 is None:
        return True
    return content_hash_of(data) == _HASH_PREFIX + descriptor.declared_sha256
