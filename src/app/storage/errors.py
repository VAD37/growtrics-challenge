"""The three ways a repository says no, whichever backend it is.

They live here rather than beside either adapter because the contract suite runs the same
assertions against both, and a test that had to know whether it was talking to a dictionary or
to Postgres to name the exception it expects would be testing the adapter rather than the port.

Three and not one. A duplicate primary key is a caller inserting twice, a version conflict is a
lost update, and a missing row is a caller acting on something that went away underneath it.
Folding them together would make every `pytest.raises` in the suite an assertion that something
went wrong rather than an assertion about what.

`RuntimeError` and not `DomainError`: these are storage failures, and translating one into an
error code a client sees is orchestration's decision. A repository that raised `DomainError`
would be answering a question it is not allowed to be asked.
"""

__all__ = ["IntegrityError", "RowNotFoundError", "StorageError", "VersionConflictError"]


class StorageError(RuntimeError):
    """Base for every refusal a repository makes. Never raised directly."""


class IntegrityError(StorageError):
    """A second row under a key that already has one, or a foreign key with nothing behind it."""


class VersionConflictError(StorageError):
    """A write whose `expected_version` no longer matches the row.

    The demo runs one worker, so this is a bug rather than contention -- which is exactly why it
    is loud. An adapter that quietly applied the write would turn a lost update into a wrong
    status nobody could trace back here.
    """


class RowNotFoundError(StorageError):
    """A write against a row that is not there."""
