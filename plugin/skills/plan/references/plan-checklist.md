# The plan checklist

Go through every item against the import document before `tm import`, and fix each miss in the document. Each item that cites a `tm guide plan` section points at the rule there, and the guide wins where the two differ; an item with no citation states its rule in full.

- [ ] **`target_repo` on every task.** It is not inherited from the plan or spec; a task without one cannot be implemented. (`tm guide plan` §1)
- [ ] **`declared_files` complete and disjoint.** Every path each task creates or modifies, tests included, and no path listed by two tasks that can run together. (`tm guide plan` §2)
- [ ] **A verification that fails before the change.** Every task carries at least one verification that checks its own deliverable, and after the import `tm verify run <task-id> --ref origin/<lands_on>` shows it failing on the landing target. (`tm guide plan` §6)
- [ ] **A joined verification on a reviewed plan.** A plan with `review: true` carries its own verification that runs its tasks' behaviour together. (`tm guide plan` §6)
- [ ] **Contracts and shared decisions in the plan's `overview`.** A shape, name or id scheme one task writes and another reads, and a ruling several tasks share, sit on the plan, where every child's brief reads them. (`tm guide plan` §2, §8)
- [ ] **An invariant names every write path.** Acceptance of an invariant or a refusal names the one function every write passes through, or each write path by name, with a check for each. (`tm guide plan` §8)
- [ ] **A bug fix replays its reproduction first.** The first acceptance line of a fix runs the reproduction the defect was reported with and expects the corrected result.
- [ ] **Limits name a config key.** A limit or default a tm config key covers is written as the project's key, never as a number. (`tm guide plan` §8)
- [ ] **A node read against a design frame `requires` its tools.** A node that builds, fixes or reviews against a design frame names in `requires` the design tool and the browser tool the project uses. (`tm guide plan` §3)
- [ ] **A migration is marked sensitive.** A node that writes a migration carries `sensitive: migration` in its frontmatter. (`tm guide plan` §2)
