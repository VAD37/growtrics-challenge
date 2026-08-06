"""Shared test material: row builders, port conformance, and the two storage backends.

Not a test package. Nothing here is collected -- no module matches `*_test.py` -- and nothing
here asserts anything on its own. It exists because the contract suite in `tests/contract/` and
the memory-double tests in `tests/unit/storage/` build the same rows, and a second copy of
`make_job` is a second definition of what a job looks like.
"""
