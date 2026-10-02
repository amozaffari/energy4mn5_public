# /compute-attribute — Propose attribution rules for unattributed jobs

Find jobs that no rule in `config/project.yaml` maps to a scientific project, and propose new
rules. Local database only.

**Step 1 — Unattributed jobs grouped by working directory:**
```bash
uv run mn5track report --project unattributed --by workdir --format json
```

**Step 2 — For the largest groups, look at job names and accounts:**
```bash
uv run mn5track report --project unattributed --by jobname-prefix --format json
uv run mn5track report --project unattributed --by account --format json
```

**Step 3 — Propose rules.** For each group worth attributing, propose one rule. Prefer
`workdir_glob` (e.g. `"*/git/<repo>*"`), then `jobname_glob`, and use `account_in` only for
dedicated allocations. Rules are ordered and first match wins, so put specific globs before
`account_in` fallbacks. Keys allowed: `project`, `workdir_glob`, `jobname_glob`, `account_in`,
`submitted_from`, `submitted_until`.

Show the user:
- A table: workdir group, jobs, billed node-h, proposed project
- The exact diff to `config/project.yaml` (new rules and any new `projects:` entries)

**Only edit `config/project.yaml` after the user confirms.** It is shared by every project
member; a rule for one member's own directory layout goes in their personal
`config/tracker.yaml` under `extra_attribution_rules` instead. Then verify:
```bash
uv run mn5track report --by project --format json
```
and report how many jobs and node-h moved out of `unattributed`. Check one moved job with
`uv run mn5track explain <jobid>` to confirm the intended rule matched.
