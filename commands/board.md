---
description: Open or export the interactive TaskManager visualizer DAG and dashboard.
argument-hint: "[open | export [output_file]]"
---

Open or export the TaskManager web visualizer:

- If `$ARGUMENTS` starts with `export`:
  Run `tm web export` (optionally specifying output path).
- Otherwise:
  Run `tm web` to start the live dashboard and auto-open it in the browser.
