---
name: compute-attribute
description: Find MareNostrum 5 jobs that no attribution rule maps to a scientific project and propose new rules for config/project.yaml, editing it only after the user confirms.
version: 1.0.0
author: Amirpasha Mozaffari
platforms: [macos, linux]
metadata:
  hermes:
    category: hpc
    tags: [hpc, marenostrum5, attribution, config]
    related_skills: [compute-report]
---

# compute-attribute: propose attribution rules for unattributed jobs

Find jobs that no rule in `config/project.yaml` maps to a project, and propose rules. Local
database only.

## When to Use

- `report --by project` shows a large `unattributed` share.
- The user starts a new project or repository and wants its jobs counted.

## Quick Reference

```bash
uv run mn5track report --project unattributed --by workdir --format json
uv run mn5track report --project unattributed --by jobname-prefix --format json
uv run mn5track report --project unattributed --by account --format json
uv run mn5track explain <jobid> --format json
```

## Procedure

1. List unattributed jobs grouped by working directory, then look at job names and accounts
   for the largest groups.
2. For each group worth attributing, propose one rule. Prefer `workdir_glob` (for example
   `"*/git/<repo>*"`), then `jobname_glob`; use `account_in` only for dedicated allocations.
   Rules are ordered and the first match wins, so put specific globs before `account_in`
   fallbacks. Allowed keys: `project`, `workdir_glob`, `jobname_glob`, `account_in`,
   `submitted_from`, `submitted_until`.
3. Show the user a table (workdir group, jobs, billed node-h, proposed project) and the exact
   diff to `config/project.yaml`, including any new `projects:` entries.
4. **Edit the file only after the user confirms the diff.**
5. Verify with `uv run mn5track report --by project --format json`: report how many jobs and
   node-h moved out of `unattributed`. Check one moved job with `explain` to confirm the
   intended rule matched.

## Pitfalls

- `config/project.yaml` is shared by every project member. A rule for one member's own
  directory layout belongs in their personal `config/tracker.yaml` under
  `extra_attribution_rules`.
- Changing shared rules changes `rules_fingerprint`; other members must use the same file and
  re-export their contributions.
- Never copy real project or allocation names into `config/project.example.yaml` or any other
  tracked file.

## Verification

The `unattributed` row in `report --by project` shrank by the expected jobs and node-h, and
`explain` names the new rule.
