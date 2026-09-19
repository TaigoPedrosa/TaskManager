# TaskManager

Local agentic task tracker system powered by SQLite and DAG heuristics.

## Features

- Multi-database architecture (`spec.db`, `runtime.db`, `ledger.db`) with SQLite WAL mode
- Fast in-engine vector search via `sqlite-vec`
- Composable qualified slug addressing
- Relational DAG heuristics and next-task recommendation
- Git worktree orchestration and verification gates

## Development

```bash
uv sync
uv run pytest
```
