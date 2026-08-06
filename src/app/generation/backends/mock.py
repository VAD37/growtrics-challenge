"""The demo's generation backend: committed lesson videos behind the real port.

A backend swap, not a bypass (scope override item 6). Nothing downstream is short-circuited:
this returns the same untrusted JSON document a rented machine would, the ACL parses it as a
claim, custody pulls the bytes through `fetch`, hashes them itself, and writes the artifact row.
Every stage still runs; only the thing that makes the video is fake.

Four properties the demo depends on:

* **The fixtures are real media.** `fixtures/lesson_a.mp4` and `fixtures/lesson_b.mp4` are
  rendered chemistry lessons with a poster and a transcript beside them, so
  `GET /v1/artifacts/{id}/content` streams something that plays and `container_is_mp4` runs
  against real bytes rather than a renamed text file.
* **The video depends on the brief.** Which of the two comes back is folded out of
  `bundle.brief_hash`, so two different jobs return two different files and the same brief
  returns the same one. A backend that answered with one hardcoded path could not tell those
  apart, and neither could a reviewer watching the demo.
* **It takes observable time, and not always the same time.** `generate` sleeps for a uniform
  draw between two bounds before answering, so a job is visibly `RUNNING` at `GENERATING` while
  a client polls it, and two jobs do not finish in lockstep. A demo whose status never changes
  proves nothing about the queue.
* **The failure path is reachable.** `FAIL_ME` anywhere in the bundle makes this backend report
  `FAILED`, which the ACL turns into `GENERATION_FAILED` and the job records as a failure that
  `GET /v1/jobs/{id}` still answers `200` for. It is a property of the fake and nothing else: no
  content is inspected, nothing is classified, and no guard lives here or is implied by it.

@TODO the real backends: local process, sandbox placement, cloud placement
(`docs/plan/07-generation.md`, `docs/open-questions.md`). None of them changes this module's
signature, which is the point of the port.
"""

import asyncio
import hashlib
import json
import random
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.config import settings
from app.domain.errors import DomainError, ErrorCode
from app.generation.ports import GenerationRequest, WorkerStatus

FIXTURE_ROOT: Final[Path] = Path(__file__).resolve().parent / "fixtures"

SAMPLE_VIDEO_PATH: Final[Path] = FIXTURE_ROOT / "sample_lesson.mp4"
"""Committed, playable, 16 KB. Regenerate with `fixtures/make_sample.sh`.

Not what this backend serves. It is the cheap stand-in for tests that need nothing more than a
well-formed MP4, so the suite does not read a megabyte to assert on an `ftyp` box.
"""

POSTER_PATH: Final[Path] = FIXTURE_ROOT / "poster.png"
TRANSCRIPT_PATH: Final[Path] = FIXTURE_ROOT / "transcript.txt"

PRIMARY_REL_PATH: Final[str] = "out/lesson.mp4"
POSTER_REL_PATH: Final[str] = "out/poster.png"
TRANSCRIPT_REL_PATH: Final[str] = "out/transcript.txt"
LOG_REL_PATH: Final[str] = "out/agent.jsonl"

PRIMARY_MEDIA_TYPE: Final[str] = "video/mp4"
POSTER_MEDIA_TYPE: Final[str] = "image/png"
TRANSCRIPT_MEDIA_TYPE: Final[str] = "text/plain"
LOG_MEDIA_TYPE: Final[str] = "application/x-ndjson"

FAIL_TOKEN: Final[str] = "FAIL_ME"
"""The one string this backend matches on, and the whole of what it does with it.

Present anywhere in the bundle, the run comes back `FAILED`. It exists so the failure path can
be demonstrated on demand rather than waited for, and it is deliberately not a guard: `intake`
owns whether a request is acceptable, and a backend deciding that would be the check in the
wrong place even if it were spelled the same way.
"""


@dataclass(frozen=True, slots=True)
class LessonFixture:
    """One committed lesson: the file, and how long it actually runs.

    `duration_s` is measured from the file and reported in the manifest as a claim. A worker's
    duration is never evidence (`custody/verifier.py` measures its own), but a fake that claimed
    a number contradicting its own bytes would make the claim/evidence gap look like a bug.
    """

    path: Path
    duration_s: float


LESSON_VIDEOS: Final[tuple[LessonFixture, ...]] = (
    LessonFixture(path=FIXTURE_ROOT / "lesson_a.mp4", duration_s=64.6),
    LessonFixture(path=FIXTURE_ROOT / "lesson_b.mp4", duration_s=82.3),
)
"""The catalog `generate` picks from. See `fixtures/README.md` for what these files are."""


def video_for_brief(
    brief_hash: str,
    videos: tuple[LessonFixture, ...] = LESSON_VIDEOS,
) -> LessonFixture:
    """Pick one lesson, deterministically, from the brief hash.

    Re-hashed rather than sliced so nothing here depends on how `brief_hash` is spelled: it is
    `sha256:<hex>` today and the only property this needs is that equal briefs give equal
    strings. The result is stable across processes, which `hash()` would not be.
    """
    digest = hashlib.sha256(brief_hash.encode("utf-8")).digest()
    return videos[digest[-1] % len(videos)]


class MockGenerationBackend:
    """A `GenerationBackend` that returns committed bytes.

    Holds one workspace per session in memory. A real backend holds a directory on a machine we
    rented; either way `fetch` is the only way bytes leave it, and it serves exactly the paths
    the manifest declared and nothing else.

    Every bound is a constructor argument so a test can drop the delay to zero without touching
    the environment. The defaults are read from settings here rather than in `main.py` because
    this is where the number means something; the composition root passes its own when it wires
    the backend up.

    @audit a filesystem-backed backend must open candidates without following symlinks. The ACL
    can only check the shape of a path string (`app/generation/acl.py`), so refusing a link is
    an obligation of whoever implements `fetch` over a real directory.
    """

    def __init__(
        self,
        *,
        videos: tuple[LessonFixture, ...] = LESSON_VIDEOS,
        poster_path: Path = POSTER_PATH,
        transcript_path: Path = TRANSCRIPT_PATH,
        delay_min_seconds: float = settings.mock_delay_min_seconds,
        delay_max_seconds: float = settings.mock_delay_max_seconds,
    ) -> None:
        self._videos: tuple[LessonFixture, ...] = videos
        self._poster_path: Path = poster_path
        self._transcript_path: Path = transcript_path
        self.delay_min_seconds: float = delay_min_seconds
        self.delay_max_seconds: float = delay_max_seconds
        self._workspaces: dict[str, dict[str, bytes]] = {}

    def next_delay_seconds(self) -> float:
        """A uniform draw inside the configured bounds.

        Named and public because "how long does a job take" is the question the demo is watched
        for, and a test asserting the bounds should not have to time a sleep to do it.
        """
        return random.uniform(self.delay_min_seconds, self.delay_max_seconds)

    async def generate(self, request: GenerationRequest) -> Mapping[str, object]:
        """Pretend to make a lesson, then hand back what a worker would claim.

        The sleep is the whole of the pretending. Everything else is a manifest: a status, four
        descriptors, and a set of check results that this backend asserts and nobody believes.
        """
        await asyncio.sleep(self.next_delay_seconds())

        if any(FAIL_TOKEN in file.text for file in request.bundle.files):
            return self._failed(request)

        video = video_for_brief(request.bundle.brief_hash, self._videos)
        files: dict[str, bytes] = {
            PRIMARY_REL_PATH: video.path.read_bytes(),
            POSTER_REL_PATH: self._poster_path.read_bytes(),
            TRANSCRIPT_REL_PATH: self._transcript_path.read_bytes(),
            LOG_REL_PATH: self._agent_log(request, video),
        }
        self._workspaces[request.session_id] = files

        media_types = {
            PRIMARY_REL_PATH: PRIMARY_MEDIA_TYPE,
            POSTER_REL_PATH: POSTER_MEDIA_TYPE,
            TRANSCRIPT_REL_PATH: TRANSCRIPT_MEDIA_TYPE,
            LOG_REL_PATH: LOG_MEDIA_TYPE,
        }
        roles = {
            PRIMARY_REL_PATH: "PRIMARY",
            POSTER_REL_PATH: "POSTER",
            TRANSCRIPT_REL_PATH: "TRANSCRIPT",
            LOG_REL_PATH: "LOG",
        }

        return {
            "session_id": request.session_id,
            "status": WorkerStatus.COMPLETED.value,
            "descriptors": [
                {
                    "role": roles[rel_path],
                    "media_type": media_types[rel_path],
                    "rel_path": rel_path,
                    "size_bytes": len(data),
                    "sha256": None,
                }
                for rel_path, data in files.items()
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
                "duration_s": video.duration_s,
                "notes": f"mock backend: committed fixture {video.path.name}, nothing was rendered",
            },
        }

    def _failed(self, request: GenerationRequest) -> Mapping[str, object]:
        """The shape a worker reports a dead run in: a status and no files.

        Still a full document, because the ACL parses this one exactly as strictly as a happy
        one and the failure has to travel the same path a real failure would.
        """
        return {
            "session_id": request.session_id,
            "status": WorkerStatus.FAILED.value,
            "descriptors": [],
            "manifest": {
                "profile": request.contract.profile_id.value,
                "contract_version": request.contract.contract_version,
                "checks": [],
                "duration_s": 0.0,
                "notes": f"mock backend: {FAIL_TOKEN} in the bundle, run reported as failed",
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

    def _agent_log(self, request: GenerationRequest, video: LessonFixture) -> bytes:
        """An operator-audience log, in the shape a real agent would append to.

        Carries the trace id and the brief hash and no learner text, so an operator can join it
        to a job without the log itself becoming a second copy of what somebody typed. The
        fixture name is on it because "which video did this job get" is the question the hash
        pick exists to answer, and answering it from the bytes alone means watching two videos.
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
                "fixture": video.path.name,
                "files": [
                    PRIMARY_REL_PATH,
                    POSTER_REL_PATH,
                    TRANSCRIPT_REL_PATH,
                    LOG_REL_PATH,
                ],
            },
        ]
        return "".join(json.dumps(line, sort_keys=True) + "\n" for line in lines).encode("utf-8")
