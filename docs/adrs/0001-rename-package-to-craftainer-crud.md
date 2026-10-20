# 0001. Rename the package to craftainer-crud

## Status

Accepted

## Context

The repository was named `tool-crud` and published to PyPI as
`craftainer-tool-crud` (import `craftainer_tool_crud`). The Craftainer
family now names repositories `lib-<name>-<lang>` and distributions
`craftainer-<name>`, with the import name following the distribution
(`craftainer_<name>`). "tool" no longer describes what the package is: it
is a library, not a tool.

PyPI cannot rename a project. A new name means a new project, and the old
one can only be left in place, given a final notice release, and archived.

## Decision

We will rename the repository to `lib-crud-py` (on GitHub; old URLs
redirect), the PyPI distribution to `craftainer-crud`, and the import name
to `craftainer_crud`. The source directory moves with `git mv`, so history
follows. The version continues: the first release under the new name is
`0.2.0`.

The old PyPI project `craftainer-tool-crud` gets a final `0.1.2` notice
release from a `legacy/0.1.x` branch, then is archived. This package ships
no compatibility alias or shim for the old import name.

## Consequences

- Consumers change their dependency to `craftainer-crud` and their imports
  to `craftainer_crud`; nothing else in the API changes.
- Old installs keep working from the archived project but get no further
  releases.
- Without an alias there is no hidden second import path to maintain, at the
  cost of a hard break for anyone who upgrades without reading the notice.
- The old name survives only in this ADR, the README's rename note, and
  internal test database names under `.devcontainer/`.
