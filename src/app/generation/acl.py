"""The anti-corruption layer. Untrusted manifest in, domain descriptors or a failure code out.

This is the seam a hostile agent attacks, so it is written as if one is on the other end. Every
rejection carries a machine-readable reason and `ErrorCode.GENERATION_FAILED`; nothing here
raises a bare exception, because a stack trace at this boundary is a defence nobody can assert
on and a message a caller cannot act on.

What it refuses, in the order it refuses them:

1. Anything pydantic will not parse: unknown fields, missing fields, a size that is not a
   number, a sha256 that is not a hash.
2. A manifest for a session we did not open, or a run the worker says failed.
3. Per descriptor: a role outside the enum, a role outside the contract, a media type outside
   that role's part, a declared size over that part's cap, and any path that is not a plain
   relative name under the harvest root.
4. Across descriptors: two claims on one path, more files than the contract allows, more of one
   role than its part allows, a declared total over the contract's cap.
5. A set with no required part in it.

Paths get the most attention because they are the only field that names something outside the
manifest. We cannot stat a workspace on a machine we do not control, so "no symlinks" is
enforced by shape rather than by inspection: a component must be a plain name, which rules out
`..`, `.`, dotfiles, `~`, drive letters, UNC prefixes, backslashes, spaces, and NUL bytes. The
harvester then asks the backend for exactly the string that survived.

`@audit` shape is not the same guarantee as `O_NOFOLLOW`. A backend serving `fetch` from a real
filesystem must refuse to follow a link even when the path looks plain, because `out/lesson.mp4`
can be a symlink to `/etc/shadow` and nothing in this module can see that. That obligation
belongs to every `GenerationBackend` implementation and is stated on the port.
"""

import re
from collections.abc import Mapping
from typing import Final

from pydantic import ValidationError

from app.domain.artifact import ArtifactDescriptor
from app.domain.contracts import HARVEST_ROOT, OutputContract, part_for_media_type
from app.domain.enums import ArtifactRole
from app.domain.errors import DomainError, ErrorCode
from app.generation.ports import GenerationOutcome, WorkerStatus

MAX_PATH_DEPTH: Final[int] = 4
"""Components allowed in a harvested path, counting the harvest root.

`out/video/lesson.mp4` passes; `out/a/b/c/lesson.mp4` does not. The video profile has no bundle
tree, so anything deeper is a worker inventing structure the contract never asked for.
"""

_COMPONENT_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
"""A plain name: starts alphanumeric, then dots, underscores, hyphens. No leading dot."""

_PATH_SEPARATOR: Final[str] = "/"


def _reject(reason: str, **details: str) -> DomainError:
    """Every refusal on this page, with its reason first."""
    return DomainError(ErrorCode.GENERATION_FAILED, {"reason": reason, **details})


def parse_outcome(payload: Mapping[str, object]) -> GenerationOutcome:
    """Parse the raw document a backend returned (D063).

    A `ValidationError` becomes `malformed_manifest` rather than escaping: pydantic's message
    quotes the input it rejected, and that input came from a worker.
    """
    try:
        return GenerationOutcome.model_validate(payload)
    except ValidationError as exc:
        raise _reject("malformed_manifest") from exc


def normalise_rel_path(raw: str) -> str:
    """Accept a plain relative path under the harvest root, or refuse it by name."""
    if raw.startswith(_PATH_SEPARATOR):
        raise _reject("absolute_path")

    components = raw.split(_PATH_SEPARATOR)
    for component in components:
        if component == "..":
            raise _reject("parent_traversal")
        if not _COMPONENT_RE.fullmatch(component):
            raise _reject("bad_component")

    if components[0] != HARVEST_ROOT:
        raise _reject("outside_harvest_root")
    if len(components) < 2:
        raise _reject("outside_harvest_root")
    if len(components) > MAX_PATH_DEPTH:
        raise _reject("path_too_deep")
    return _PATH_SEPARATOR.join(components)


def _role_of(raw: str) -> ArtifactRole:
    try:
        return ArtifactRole(raw)
    except ValueError as exc:
        raise _reject("unknown_role") from exc


def accept_descriptors(
    outcome: GenerationOutcome,
    contract: OutputContract,
    *,
    expected_session_id: str,
) -> tuple[ArtifactDescriptor, ...]:
    """Turn a worker's claims into domain descriptors, or refuse the whole set.

    All or nothing on purpose. A partially accepted manifest would mean harvesting the files a
    worker got right while ignoring the ones it did not, and "which of these is the lesson" is
    not a question to answer from a set somebody already proved they cannot produce correctly.
    """
    if outcome.session_id != expected_session_id:
        raise _reject("session_mismatch")
    if outcome.status is WorkerStatus.FAILED:
        raise _reject("worker_reported_failure")

    accepted: list[ArtifactDescriptor] = []
    for raw in outcome.descriptors:
        role = _role_of(raw.role)
        part = part_for_media_type(contract, raw.media_type)
        if part is None or part.role is not role:
            # One reason for both, because they are one question: does this media type belong to
            # the part this file claims to be. A worker declaring `image/png` as the PRIMARY has
            # named a type the contract knows and a part it does not belong to.
            if not any(item.role is role for item in contract.parts):
                raise _reject("role_not_in_contract", role=role.value)
            raise _reject("media_type_not_in_part", role=role.value)
        if raw.size_bytes > part.max_bytes:
            raise _reject("declared_size_over_cap", role=role.value)
        accepted.append(
            ArtifactDescriptor(
                role=role,
                audience=part.audience,
                media_type=raw.media_type,
                rel_path=normalise_rel_path(raw.rel_path),
                declared_size_bytes=raw.size_bytes,
                declared_sha256=raw.sha256,
            )
        )

    _assert_set_fits_contract(tuple(accepted), contract)
    return tuple(accepted)


def _assert_set_fits_contract(
    descriptors: tuple[ArtifactDescriptor, ...],
    contract: OutputContract,
) -> None:
    """The checks that only make sense over the whole set."""
    paths = [item.rel_path for item in descriptors]
    if len(set(paths)) != len(paths):
        raise _reject("duplicate_path")

    if len(descriptors) > contract.max_file_count:
        raise _reject("too_many_files", declared=str(len(descriptors)))

    for part in contract.parts:
        count = sum(1 for item in descriptors if item.role is part.role)
        if count > part.max_count:
            raise _reject("too_many_for_role", role=part.role.value)
        if part.required and count == 0:
            raise _reject("missing_required_part", role=part.role.value)

    declared_total = sum(item.declared_size_bytes for item in descriptors)
    if declared_total > contract.total_max_bytes:
        raise _reject("declared_total_over_cap", declared=str(declared_total))
