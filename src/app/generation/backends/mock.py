"""The demo's generation backend: a committed sample video behind the real port.

A backend swap, not a bypass (scope override item 6). Nothing downstream is short-circuited:
this returns the same untrusted JSON document a rented machine would, the ACL parses it as a
claim, custody pulls the bytes through `fetch`, hashes them itself, and writes the artifact row.
Every stage still runs; only the thing that makes the video is fake.

Two properties the demo depends on:

* **The fixture is a real MP4.** `fixtures/sample_lesson.mp4` has an `ftyp` box, an h264 track,
  and an AAC track, so `GET /v1/artifacts/{id}/content` streams something that plays and
  `container_is_mp4` runs against real bytes.
* **It takes observable time.** `generate` sleeps before answering, so a job is visibly `RUNNING`
  before it is `SUCCEEDED`. A demo whose status never changes proves nothing about the queue.

@TODO the real backends: local process, sandbox placement, cloud placement
(`docs/plan/07-generation.md`, `docs/open-questions.md`). None of them changes this module's
signature, which is the point of the port.
"""

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from app.domain.errors import DomainError, ErrorCode
from app.generation.ports import GenerationRequest, WorkerStatus

FIXTURE_ROOT: Final[Path] = Path(__file__).resolve().parent / "fixtures"
SAMPLE_VIDEO_PATH: Final[Path] = FIXTURE_ROOT / "sample_lesson.mp4"
"""Committed, playable, 16 KB. Regenerate with `fixtures/make_sample.sh`."""

PRIMARY_REL_PATH: Final[str] = "out/lesson.mp4"
LOG_REL_PATH: Final[str] = "out/agent.jsonl"

MOCK_DELAY_SECONDS: Final[float] = 1.5
"""Non-zero on purpose. See the module docstring.

@TODO read this from settings once `main.py` wires the backend
(`APP_MOCK_GENERATION_DELAY_SECONDS`); it is a constructor argument so that a test can drop it
to milliseconds without touching the environment.
"""


class MockGenerationBackend:
    """A `GenerationBackend` that returns a fixture.

    Holds one workspace per session in memory. A real backend holds a directory on a machine we
    rented; either way `fetch` is the only way bytes leave it, and it serves exactly the paths
    the manifest declared and nothing else.

    @audit a filesystem-backed backend must open candidates without following symlinks. The ACL
    can only check the shape of a path string (`app/generation/acl.py`), so refusing a link is
    an obligation of whoever implements `fetch` over a real directory.
    """

    def __init__(
        self,
        *,
        fixture_path: Path = SAMPLE_VIDEO_PATH,
        delay_seconds: float = MOCK_DELAY_SECONDS,
    ) -> None:
        self._fixture_path: Path = fixture_path
        self._delay_seconds: float = delay_seconds
        self._workspaces: dict[str, dict[str, bytes]] = {}

    async def generate(self, request: GenerationRequest) -> Mapping[str, object]:
        """Pretend to make a lesson, then hand back what a worker would claim.

        The sleep is the whole of the pretending. Everything else is a manifest: a status, two
        descriptors, and a set of check results that this backend asserts and nobody believes.
        """
        await asyncio.sleep(self._delay_seconds)

        video = self._fixture_path.read_bytes()
        log = self._agent_log(request)
        self._workspaces[request.session_id] = {
            PRIMARY_REL_PATH: video,
            LOG_REL_PATH: log,
        }

        return {
            "session_id": request.session_id,
            "status": WorkerStatus.COMPLETED.value,
            "descriptors": [
                {
                    "role": "PRIMARY",
                    "media_type": "video/mp4",
                    "rel_path": PRIMARY_REL_PATH,
                    "size_bytes": len(video),
                    "sha256": None,
                },
                {
                    "role": "LOG",
                    "media_type": "application/x-ndjson",
                    "rel_path": LOG_REL_PATH,
                    "size_bytes": len(log),
                    "sha256": None,
                },
            ],
            "manifest": {
                "profile": request.contract.profile_id.value,
                "contract_version": request.contract.contract_version,
                # Claims, not evidence. The validator runs its own chain over the bytes we pull,
                # and a worker asserting `audio_not_silent` proves nothing about the audio.
                "checks": [
                    {"name": "container_is_mp4", "passed": True},
                    {"name": "video_stream_present", "passed": True},
                    {"name": "audio_stream_present", "passed": True},
                    {"name": "audio_not_silent", "passed": True},
                ],
                "duration_s": 4.0,
                "notes": "mock backend: committed fixture, no lesson was generated",
            },
        }

    async def fetch(self, session_id: str, rel_path: str, *, max_bytes: int) -> bytes:
        """Serve one declared candidate, cut off one byte past the cap.

        Returning `max_bytes + 1` rather than raising lets the harvester be the one place that
        decides an oversize file is fatal, and keeps that decision testable against a backend
        that lies about size.
        """
        workspace = self._workspaces.get(session_id)
        if workspace is None:
            raise DomainError(
                ErrorCode.GENERATION_FAILED,
                {"reason": "unknown_session"},
            )
        data = workspace.get(rel_path)
        if data is None:
            raise DomainError(
                ErrorCode.GENERATION_FAILED,
                {"reason": "unknown_candidate", "rel_path": rel_path},
            )
        return data[: max_bytes + 1]

    def _agent_log(self, request: GenerationRequest) -> bytes:
        """An operator-audience log, in the shape a real agent would append to.

        Carries the trace id and the brief hash and no learner text, so an operator can join it
        to a job without the log itself becoming a second copy of what somebody typed.
        """
        lines = [
            {
                "at": "1970-01-01T00:00:00Z",
                "trace_id": request.trace_id,
                "event": "session.start",
                "brief_hash": request.bundle.brief_hash,
                "template_version": request.bundle.template_version,
            },
            {
                "at": "1970-01-01T00:00:04Z",
                "trace_id": request.trace_id,
                "event": "session.end",
                "backend": "mock",
                "files": [PRIMARY_REL_PATH],
            },
        ]
        return "".join(json.dumps(line, sort_keys=True) + "\n" for line in lines).encode("utf-8")
