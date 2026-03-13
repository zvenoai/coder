"""Tests for ADR (Architecture Decision Record) module."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from orchestrator.adr import create_adr, list_adrs, read_adr, slugify


class TestSlugify:
    """Tests for slugify function."""

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            (
                "Use PostgreSQL for persistence",
                "use-postgresql-for-persistence",
            ),
            (
                "Adopt Hexagonal Architecture",
                "adopt-hexagonal-architecture",
            ),
            (
                "  Extra   Spaces  ",
                "extra-spaces",
            ),
            (
                "Special!@#Characters$%^Here",
                "specialcharactershere",
            ),
            (
                "Already-kebab-case",
                "already-kebab-case",
            ),
            (
                "UPPER CASE TITLE",
                "upper-case-title",
            ),
        ],
    )
    def test_slugify_converts_title(self, title: str, expected: str) -> None:
        assert slugify(title) == expected


class TestCreateAdr:
    """Tests for create_adr function."""

    def test_creates_file(self, tmp_path: Path) -> None:
        path = create_adr(
            title="Use PostgreSQL",
            context="We need a database.",
            decision="Use PostgreSQL.",
            consequences="Must manage migrations.",
            adr_dir=str(tmp_path),
        )
        assert Path(path).exists()

    def test_correct_format(self, tmp_path: Path) -> None:
        path = create_adr(
            title="Use PostgreSQL",
            context="We need a database.",
            decision="Use PostgreSQL.",
            consequences="Must manage migrations.",
            adr_dir=str(tmp_path),
        )
        content = Path(path).read_text()
        assert "# Use PostgreSQL" in content
        assert "## Date" in content
        assert "## Status" in content
        assert "## Context" in content
        assert "## Decision" in content
        assert "## Consequences" in content
        assert "accepted" in content
        assert "We need a database." in content
        assert "Use PostgreSQL." in content
        assert "Must manage migrations." in content

    def test_custom_status(self, tmp_path: Path) -> None:
        path = create_adr(
            title="Experiment with Redis",
            context="Cache layer needed.",
            decision="Try Redis.",
            consequences="Extra infra.",
            status="proposed",
            adr_dir=str(tmp_path),
        )
        content = Path(path).read_text()
        assert "proposed" in content

    def test_date_format_in_filename(self, tmp_path: Path) -> None:
        path = create_adr(
            title="Use PostgreSQL",
            context="ctx",
            decision="dec",
            consequences="cons",
            adr_dir=str(tmp_path),
        )
        filename = Path(path).name
        assert re.match(r"\d{4}-\d{2}-\d{2}-use-postgresql\.md", filename)

    def test_date_in_body(self, tmp_path: Path) -> None:
        path = create_adr(
            title="Use PostgreSQL",
            context="ctx",
            decision="dec",
            consequences="cons",
            adr_dir=str(tmp_path),
        )
        content = Path(path).read_text()
        assert re.search(r"\d{4}-\d{2}-\d{2}", content)

    def test_creates_directory_if_missing(self, tmp_path: Path) -> None:
        nested = tmp_path / "sub" / "dir"
        path = create_adr(
            title="Test",
            context="ctx",
            decision="dec",
            consequences="cons",
            adr_dir=str(nested),
        )
        assert Path(path).exists()


class TestListAdrs:
    """Tests for list_adrs function."""

    def test_returns_existing(self, tmp_path: Path) -> None:
        create_adr(
            title="First ADR",
            context="ctx",
            decision="dec",
            consequences="cons",
            adr_dir=str(tmp_path),
        )
        create_adr(
            title="Second ADR",
            context="ctx",
            decision="dec",
            consequences="cons",
            status="proposed",
            adr_dir=str(tmp_path),
        )
        result = list_adrs(adr_dir=str(tmp_path))
        assert len(result) == 2
        titles = {r["title"] for r in result}
        assert "First ADR" in titles
        assert "Second ADR" in titles
        for item in result:
            assert "filename" in item
            assert "title" in item
            assert "date" in item
            assert "status" in item

    def test_empty_dir(self, tmp_path: Path) -> None:
        result = list_adrs(adr_dir=str(tmp_path))
        assert result == []

    def test_nonexistent_dir(self, tmp_path: Path) -> None:
        result = list_adrs(adr_dir=str(tmp_path / "nonexistent"))
        assert result == []


class TestReadAdr:
    """Tests for read_adr function."""

    def test_returns_content(self, tmp_path: Path) -> None:
        path = create_adr(
            title="Readable ADR",
            context="Some context.",
            decision="A decision.",
            consequences="Some consequences.",
            adr_dir=str(tmp_path),
        )
        filename = Path(path).name
        content = read_adr(filename, adr_dir=str(tmp_path))
        assert "# Readable ADR" in content
        assert "Some context." in content

    def test_nonexistent_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            read_adr("nonexistent.md", adr_dir=str(tmp_path))


class TestListAdrsParsingVariants:
    """Bug: ADR section parsing fragile with blank lines."""

    def test_hand_edited_adr_without_blank_line(
        self,
        tmp_path: Path,
    ) -> None:
        """ADR with no blank line between header and value."""
        content = "# Hand Edited\n\n## Date\n2025-01-15\n\n## Status\naccepted\n\n## Context\nSome context.\n"
        (tmp_path / "2025-01-15-hand-edited.md").write_text(content)

        result = list_adrs(adr_dir=str(tmp_path))
        assert len(result) == 1
        assert result[0]["date"] == "2025-01-15"
        assert result[0]["status"] == "accepted"

    def test_template_adr_with_blank_line(
        self,
        tmp_path: Path,
    ) -> None:
        """ADR from create_adr has blank line between header and value."""
        create_adr(
            title="Template ADR",
            context="ctx",
            decision="dec",
            consequences="cons",
            adr_dir=str(tmp_path),
        )

        result = list_adrs(adr_dir=str(tmp_path))
        assert len(result) == 1
        assert result[0]["date"] != ""
        assert result[0]["status"] == "accepted"


class TestListAdrsExcludesNonAdr:
    """Bug: list_adrs uses glob('*.md') which matches README.md."""

    def test_excludes_readme(self, tmp_path: Path) -> None:
        # Create a real ADR
        create_adr(
            title="Real ADR",
            context="ctx",
            decision="dec",
            consequences="cons",
            adr_dir=str(tmp_path),
        )
        # Create a README.md that is NOT an ADR
        (tmp_path / "README.md").write_text("# ADR Directory\n\nDocs here.\n")

        result = list_adrs(adr_dir=str(tmp_path))

        filenames = [r["filename"] for r in result]
        assert "README.md" not in filenames
        assert len(result) == 1
        assert result[0]["title"] == "Real ADR"
