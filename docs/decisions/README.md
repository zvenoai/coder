# Architecture Decision Records (ADRs)

This directory contains Architecture Decision Records for the ZvenoAI Coder project.

## Format

Each ADR is a markdown file named `YYYY-MM-DD-<slug>.md` with the following sections:

- **Title** — short description of the decision
- **Date** — when the decision was made
- **Status** — `proposed`, `accepted`, `deprecated`, or `superseded`
- **Context** — problem context and forces at play
- **Decision** — the decision made
- **Consequences** — expected consequences (positive and negative)

## Creating ADRs

The supervisor agent can create ADRs via the `create_adr` MCP tool. ADRs can also be created manually by adding a markdown file following the format above.

## Listing and Reading

Use `list_adrs` to see all existing ADRs, and `read_adr` to read a specific one by filename.
