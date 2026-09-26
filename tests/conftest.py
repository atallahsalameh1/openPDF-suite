"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).parent.parent / "src"
TESTS = Path(__file__).parent
for p in (str(SRC), str(TESTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

from fixtures.make_fixtures import FIXTURE_DIR, build_all, make_long  # noqa: E402


@pytest.fixture(scope="session")
def fixture_dir() -> Path:
    return build_all(FIXTURE_DIR)


@pytest.fixture(scope="session")
def long_pdf(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("long") / "long.pdf"
    make_long(out, pages=120)
    return out


@pytest.fixture()
def standard_pdf(fixture_dir, tmp_path) -> Path:
    """A writable copy of standard.pdf (edits must never touch the fixture)."""
    dst = tmp_path / "standard_copy.pdf"
    dst.write_bytes((fixture_dir / "standard.pdf").read_bytes())
    return dst


@pytest.fixture()
def colored_pdf(fixture_dir, tmp_path) -> Path:
    dst = tmp_path / "colored_copy.pdf"
    dst.write_bytes((fixture_dir / "colored_bg.pdf").read_bytes())
    return dst


@pytest.fixture()
def unicode_pdf(fixture_dir, tmp_path) -> Path:
    dst = tmp_path / "unicode_copy.pdf"
    dst.write_bytes((fixture_dir / "unicode.pdf").read_bytes())
    return dst


@pytest.fixture()
def image_pdf(fixture_dir, tmp_path) -> Path:
    dst = tmp_path / "image_copy.pdf"
    dst.write_bytes((fixture_dir / "image_behind.pdf").read_bytes())
    return dst


@pytest.fixture()
def columns_pdf(fixture_dir, tmp_path) -> Path:
    dst = tmp_path / "columns_copy.pdf"
    dst.write_bytes((fixture_dir / "columns.pdf").read_bytes())
    return dst


@pytest.fixture()
def tight_pdf(fixture_dir, tmp_path) -> Path:
    dst = tmp_path / "tight_copy.pdf"
    dst.write_bytes((fixture_dir / "tight.pdf").read_bytes())
    return dst


@pytest.fixture()
def ruled_table_pdf(fixture_dir, tmp_path) -> Path:
    dst = tmp_path / "ruled_table_copy.pdf"
    dst.write_bytes((fixture_dir / "ruled_table.pdf").read_bytes())
    return dst


@pytest.fixture()
def wrapped_table_pdf(fixture_dir, tmp_path) -> Path:
    dst = tmp_path / "wrapped_table_copy.pdf"
    dst.write_bytes((fixture_dir / "wrapped_table.pdf").read_bytes())
    return dst


@pytest.fixture()
def rotated_pdf(fixture_dir, tmp_path) -> Path:
    dst = tmp_path / "rotated_copy.pdf"
    dst.write_bytes((fixture_dir / "rotated.pdf").read_bytes())
    return dst
