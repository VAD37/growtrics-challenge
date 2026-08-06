"""Step 4 of 4: the verified primary becomes visible to a learner.

Stage `PUBLISHING`, 95 percent. The step marks the artifact; the runner writes the terminal
status and `jobs.artifact_id` immediately after, because only orchestration moves a job (D066).
"""

from app.domain.records import ArtifactRecord
from app.orchestration.ports import ArtifactWriter


async def run_publish(artifacts: ArtifactWriter, *, primary: ArtifactRecord) -> ArtifactRecord:
    """Stamp `published_at` on the primary artifact and return the row as it now stands.

    Two writes rather than one, on purpose: `artifacts.published_at` is custody's column and
    `jobs.artifact_id` is orchestration's, and each is written by its owner. The step returns the
    row so the runner names the artifact it just saw published rather than the one it was
    holding from an earlier stage.

    @TODO the sidecar roles (`POSTER`, `TRANSCRIPT`, `CAPTIONS`) are stored by harvest and are
    not published here. `docs/demo.md` cuts the deliverable endpoint, so nothing lists them yet;
    when `GET /v1/jobs/{id}/deliverable` arrives this publishes the set rather than the primary.
    """
    return await artifacts.publish(primary)
