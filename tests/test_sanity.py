"""Sanity check: the package imports and exposes a version."""

import lsa


def test_version() -> None:
    assert lsa.__version__
