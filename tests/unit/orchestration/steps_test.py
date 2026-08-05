"""The four steps, one module each.

A step is thin by design: typed in, one port call, typed out. The interesting property is not
what any one of them computes, it is that none of them reaches sideways. D065 says a step shares
types with another step through `domain` and in no other way, and the last test in this file
reads the import statements to prove it rather than trusting the convention.
"""

import ast
from pathlib import Path
from typing import Final

import pytest
from fakes_test import (
    ARTIFACT_ID,
    JOB_ID,
    SESSION_ID,
    TRACE_ID,
    FakeArtifactWriter,
    FakeBriefWriter,
    FakeGenerationGateway,
    make_artifact,
    make_brief,
    make_job,
    make_outcome,
    make_request,
)

from app.orchestration import steps
from app.orchestration.steps.generate import run_generate
from app.orchestration.steps.harvest import run_harvest
from app.orchestration.steps.intake import run_intake
from app.orchestration.steps.publish import run_publish

STEP_MODULES: Final[tuple[str, ...]] = ("intake", "generate", "harvest", "publish")


async def test_intake_seals_the_brief_for_the_job() -> None:
    writer = FakeBriefWriter()
    brief = await run_intake(writer, job=make_job(), request=make_request())

    assert writer.calls == [JOB_ID]
    assert brief.job_id == JOB_ID
    assert brief.brief_id == make_brief().brief_id


async def test_generate_hands_the_worker_only_a_session_id() -> None:
    """`plan/12-data-control.md`: the worker never learns the job identity."""
    gateway = FakeGenerationGateway()
    outcome = await run_generate(
        gateway,
        job=make_job(),
        brief=make_brief(),
        session_id=SESSION_ID,
        trace_id=TRACE_ID,
    )

    assert gateway.calls == [SESSION_ID]
    assert outcome.descriptors[0].media_type == "video/mp4"


async def test_harvest_gives_custody_the_denormalised_columns_it_must_copy() -> None:
    """A6 and D073: `principal_id` and `chat_context_id` are copied at insert, not derived."""
    writer = FakeArtifactWriter()
    harvested = await run_harvest(writer, job=make_job(), outcome=make_outcome())

    assert writer.harvested == [JOB_ID]
    assert harvested.primary.principal_id == "u_demo"
    assert harvested.primary.artifact_id == ARTIFACT_ID


async def test_publish_marks_the_primary_and_returns_it() -> None:
    writer = FakeArtifactWriter()
    published = await run_publish(writer, primary=make_artifact())

    assert writer.published == [ARTIFACT_ID]
    assert published.published_at is not None


@pytest.mark.parametrize("name", STEP_MODULES)
def test_no_step_imports_another_step(name: str) -> None:
    """D065, checked rather than promised. Steps share types through `domain` and nothing else."""
    source = Path(steps.__file__).with_name(f"{name}.py")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)

    siblings = {f"app.orchestration.steps.{other}" for other in STEP_MODULES} - {
        f"app.orchestration.steps.{name}"
    }
    assert not siblings.intersection(imported)
    assert not any(module.startswith("app.storage") for module in imported)
    assert not any(module.startswith("app.api") for module in imported)


@pytest.mark.parametrize("name", STEP_MODULES)
def test_a_step_is_one_public_function(name: str) -> None:
    """One module, one async entry point. A step that grew a second one has become a service."""
    source = Path(steps.__file__).with_name(f"{name}.py")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    public = [
        node.name
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        and not node.name.startswith("_")
    ]
    assert public == [f"run_{name}"]
