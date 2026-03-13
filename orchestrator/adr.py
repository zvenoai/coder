"""Architecture Decision Record (ADR) management.

Provides functions to create, list, and read ADR documents
stored as markdown files in a configurable directory.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

ADR_DIR = "docs/decisions"

_ADR_TEMPLATE = """\
# {title}

## Date

{date}

## Status

{status}

## Context

{context}

## Decision

{decision}

## Consequences

{consequences}
"""


def slugify(title: str) -> str:
    """Convert a title to a kebab-case slug.

    Args:
        title: Human-readable title string.

    Returns:
        Lowercase kebab-case slug with non-alphanumeric
        characters removed.
    """
    text = title.lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


def create_adr(
    title: str,
    context: str,
    decision: str,
    consequences: str,
    status: str = "accepted",
    adr_dir: str = ADR_DIR,
) -> str:
    """Create a new ADR markdown file.

    Args:
        title: ADR title.
        context: Problem context and forces at play.
        decision: The decision made.
        consequences: Expected consequences of the decision.
        status: ADR status (default: "accepted").
        adr_dir: Directory to store ADR files.

    Returns:
        Absolute path to the created ADR file.
    """
    dir_path = Path(adr_dir)
    dir_path.mkdir(parents=True, exist_ok=True)

    today = date.today().isoformat()
    slug = slugify(title)
    filename = f"{today}-{slug}.md"
    filepath = dir_path / filename

    content = _ADR_TEMPLATE.format(
        title=title,
        date=today,
        status=status,
        context=context,
        decision=decision,
        consequences=consequences,
    )
    filepath.write_text(content)
    return str(filepath)


def list_adrs(
    adr_dir: str = ADR_DIR,
) -> list[dict[str, str]]:
    """List all ADR files in the directory.

    Args:
        adr_dir: Directory containing ADR files.

    Returns:
        List of dicts with keys: filename, title, date,
        status. Empty list if directory does not exist.
    """
    dir_path = Path(adr_dir)
    if not dir_path.is_dir():
        return []

    # ADR files follow the pattern YYYY-MM-DD-slug.md
    adr_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}-.+\.md$")
    results: list[dict[str, str]] = []
    for path in sorted(dir_path.glob("*.md")):
        if not adr_pattern.match(path.name):
            continue
        content = path.read_text()
        title = ""
        adr_date = ""
        status = ""
        for line in content.splitlines():
            if line.startswith("# ") and not title:
                title = line[2:].strip()
                continue
        # Extract date and status from sections.
        sections = re.split(r"^## ", content, flags=re.MULTILINE)
        for section in sections:
            lines = section.split("\n")
            # Find the first non-empty line after the header
            value = ""
            for ln in lines[1:]:
                stripped = ln.strip()
                if stripped:
                    value = stripped
                    break
            if section.startswith("Date"):
                adr_date = value
            elif section.startswith("Status"):
                status = value
        results.append(
            {
                "filename": path.name,
                "title": title,
                "date": adr_date,
                "status": status,
            }
        )
    return results


def read_adr(
    filename: str,
    adr_dir: str = ADR_DIR,
) -> str:
    """Read the content of an ADR file.

    Args:
        filename: Name of the ADR file (e.g.,
            "2024-01-15-use-postgresql.md").
        adr_dir: Directory containing ADR files.

    Returns:
        The full text content of the ADR.

    Raises:
        FileNotFoundError: If the ADR file does not exist.
    """
    filepath = Path(adr_dir) / filename
    if not filepath.is_file():
        msg = f"ADR not found: {filename}"
        raise FileNotFoundError(msg)
    return filepath.read_text()
