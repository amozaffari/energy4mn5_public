---
name: compute-share
description: Export your aggregated MareNostrum 5 usage for a project, or import other members' contribution files, to rebuild the project-wide total that no single member can see.
version: 1.0.0
author: Amirpasha Mozaffari
platforms: [macos, linux]
metadata:
  hermes:
    category: hpc
    tags: [hpc, marenostrum5, collaboration]
    related_skills: [compute-statement, compute-report]
---

# compute-share: exchange project usage with other members

Each member can only see their own MN5 jobs, so a project's full total is rebuilt from every
member's contribution file. A file holds aggregated totals (month × allocation × partition ×
state × queue) plus the member's username: no job names, paths or job IDs. Local only.

## When to Use

- "Export my usage for project X" (export).
- The user received a colleague's file or folder of files (import).
- "Whose usage is already included?" (list).

## Quick Reference

```bash
uv run mn5track sync                              # so an export includes the latest jobs
uv run mn5track export <project>                  # → contributions/<project>/<user>.json
uv run mn5track import <file-or-dir> --format json
uv run mn5track contributors --format json
uv run mn5track project-lifetime <project> --format json
```

## Procedure

1. Work out the action from the user's request: export (a project name only), import (a path),
   or list (nothing else given).
2. **Export:** sync, then export. Give the user the path, and remind them to share the file
   only through a private channel: a private repository, the project's GPFS directory or a
   direct message.
3. **Import:** import the file or folder, then list contributors. Report per file: member,
   project and status (`imported`, `skipped: own`, `skipped: not newer`, `invalid`), and every
   warning verbatim.
4. After an import, run `project-lifetime` and give the new `lower_bound_gpu_hours_billed` and
   `coverage` (members with detailed data vs bsc_acct only).

## Pitfalls

- "different rules" means that member uses another `config/project.yaml`; they must take the
  shared one and re-export.
- A khours mismatch against bsc_acct usually means the file is older than our snapshot.
- Contribution files contain a username. Never commit them (`contributions/` is gitignored),
  never paste their contents into a statement, and never send them to a public channel.

## Verification

`contributors` lists the imported member with the expected export date.
