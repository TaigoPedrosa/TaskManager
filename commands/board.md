---
description: Open or export the interactive TaskManager visualizer DAG and dashboard.
argument-hint: "[open | export [output-file]]"
---

Show the task graph and dashboard. Neither form changes anything in the database.

- `$ARGUMENTS` starts with `export`: write a standalone HTML file and report its path.
  `tm web export -o <output-file>` — the output path is an option, not a positional argument, and
  defaults to `spec-dashboard.html` in the current directory. Prefer this form: it needs no server.
- otherwise: `tm web --port 6701` serves the live dashboard and opens a browser (`--no-open`
  suppresses that, `-C <project root>` picks the project). It **blocks the shell until stopped** and
  auto-switches to the next free port when 6701 is taken, printing the URL it settled on. Start it
  in the background only with its PID recorded, and stop that PID before going idle.

For a state you want to read rather than look at, no server is needed:
`tm task list --yaml`, `tm run list --yaml`, `tm decision list --status open`.
