"""Every setting reaches a container, and every knob an operator can see does something.

`config.py` is the only reader of the environment, `docker-compose.yml` is what supplies it, and
`.env.example` is what an operator reads before deciding what to change. Those three drift the
moment a lane adds a setting, and the failure is quiet in the worst way: the operator edits
`.env`, compose does not pass the name through, the container falls back to the Python default,
and the number they set is the number they already had. Nothing errors. Nothing logs.

So this file asserts the three agree, in both directions:

* Every field on `Settings` is passed through in `docker-compose.yml` and documented in
  `.env.example`, unless it is on one of the two lists below with a reason.
* Neither file names an `APP_*` variable that `Settings` does not read, which is the same bug
  seen from the other end: a knob in `.env.example` that turns nothing.

Read as text. A YAML parser is not a dependency this repository has, and the question here is
which names appear, which does not need a grammar.
"""

import re
from pathlib import Path
from typing import Final

from app.config import Settings

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_COMPOSE: Final[str] = (_REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
_ENV_EXAMPLE: Final[str] = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")

_APP_VAR_RE: Final[re.Pattern[str]] = re.compile(r"\bAPP_[A-Z0-9_]+\b")

DERIVED_IN_COMPOSE: Final[frozenset[str]] = frozenset(
    {
        "APP_DATABASE_URL",
        "APP_OBJECT_STORE_ENDPOINT",
        "APP_OBJECT_STORE_ACCESS_KEY",
        "APP_OBJECT_STORE_SECRET_KEY",
    }
)
"""Built by compose out of `POSTGRES_*` and `MINIO_*`, so `.env.example` names those instead.

Setting `APP_DATABASE_URL` by hand would let the app point at one database while the `db`
container created another, and the credentials have to match the containers that hold them.
"""

BAKED_INTO_THE_IMAGE: Final[frozenset[str]] = frozenset({"APP_VERSION"})
"""Reported, not configured. An operator who changes what version the app claims to be has made
`/health` lie, which is the opposite of what that endpoint is for."""


def settings_variables() -> frozenset[str]:
    return frozenset(f"APP_{name.upper()}" for name in Settings.model_fields)


def named_in(text: str) -> frozenset[str]:
    return frozenset(_APP_VAR_RE.findall(text))


def test_compose_passes_every_setting_through() -> None:
    """A setting compose does not pass is one `.env` cannot change: the container sees the
    Python default and the operator sees no effect."""
    expected = settings_variables() - DERIVED_IN_COMPOSE - BAKED_INTO_THE_IMAGE
    assert expected <= named_in(_COMPOSE), (
        f"docker-compose.yml does not pass: {sorted(expected - named_in(_COMPOSE))}"
    )


def test_the_example_env_documents_every_setting() -> None:
    """`.env.example` is the whole list of knobs. One missing is one nobody finds."""
    expected = settings_variables() - DERIVED_IN_COMPOSE - BAKED_INTO_THE_IMAGE
    assert expected <= named_in(_ENV_EXAMPLE), (
        f".env.example does not document: {sorted(expected - named_in(_ENV_EXAMPLE))}"
    )


def test_neither_file_offers_a_knob_that_turns_nothing() -> None:
    """The same drift from the other side. A leftover `APP_*` after a setting is renamed reads
    as a working control and is not one."""
    known = settings_variables()
    assert named_in(_COMPOSE) <= known, (
        f"docker-compose.yml sets what config.py does not read: "
        f"{sorted(named_in(_COMPOSE) - known)}"
    )
    assert named_in(_ENV_EXAMPLE) <= known, (
        f".env.example documents what config.py does not read: "
        f"{sorted(named_in(_ENV_EXAMPLE) - known)}"
    )


def test_the_derived_names_are_really_derived() -> None:
    """The exemption list is only honest while compose still builds these itself. If one moves
    to a plain passthrough it is a normal setting again and belongs in `.env.example`."""
    for name in DERIVED_IN_COMPOSE:
        assert name in _COMPOSE, f"{name} is exempt from .env.example but compose does not set it"
        assert f"${{{name}" not in _COMPOSE, (
            f"{name} is interpolated from the environment, so it is not derived: drop it from "
            "DERIVED_IN_COMPOSE and document it in .env.example"
        )
