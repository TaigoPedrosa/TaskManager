# Contributing

## Setup

macOS or Linux, with [uv](https://docs.astral.sh/uv/) (it fetches Python 3.14), git and Node 22.

```bash
uv sync
```

The stylesheet test rebuilds `src/taskmanager/web/static/tailwind.css` with the Tailwind
standalone CLI v3.4.19 and fails without it. The README's "Rebuilding the web stylesheet" shows
where to fetch it and how the test finds it.

## Gates

A change merges once all of these pass, the same commands CI runs:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
node --test tests/
```

A behaviour change comes with a test that fails without it.

## Running your checkout's `tm`

Manage your own work with an installed `tm`. From a git worktree, the checkout's `uv run tm`
finds the primary checkout's `.taskmanager` and runs the code under development against it, so
point it at a scratch copy with `-C <dir>`.

## Pull requests

- Open them against `main`.
- Add a line under `## [Unreleased]` in `CHANGELOG.md` for anything a user or an agent would
  notice.
- Commit subjects take a conventional prefix: `feat`, `fix`, `docs`, `test`, `refactor`,
  `chore`.

## Releases

A release sets one version in every manifest `tests/unit/test_package.py` checks, renames
`## [Unreleased]` in `CHANGELOG.md` to `## [<version>] - <date>`, and tags the landed commit
`v<version>`.
