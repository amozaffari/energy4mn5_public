---
name: compute-statement
description: Produce the computing-resources paragraph and table for a paper's acknowledgements or a proposal's previous-usage section, from the local mn5track database.
version: 1.0.0
author: Amirpasha Mozaffari
platforms: [macos, linux]
metadata:
  hermes:
    category: hpc
    tags: [hpc, marenostrum5, writing, acknowledgements]
    related_skills: [compute-report, compute-share]
---

# compute-statement: resource-usage paragraph for a paper or proposal

Produce the "computing resources" paragraph and table for one scientific project.

## When to Use

The user is writing a paper, report or proposal and needs the compute used by a project, an
acknowledgement sentence, or a previous-usage table.

## Quick Reference

```bash
uv run mn5track statement <project> --format json
```

## Procedure

1. Take the project from the user's request. If none is given, list the projects in
   `config/project.yaml` and ask which one.
2. Run the command from the repository root.
3. Show the `markdown` field verbatim, then add at most three lines:
   1. The period covered and the date of the newest bsc_acct snapshot it relies on.
   2. Coverage (`lifetime.coverage`): members with detailed data vs bsc_acct totals only, and
      `lifetime.invisible_allocations`, so the total is a lower bound. Suggest
      `compute-share` if few members have contributed.
   3. A reminder that the acknowledgement sentence is a draft; the grant agreement's mandated
      wording wins.

## Pitfalls

- Do not edit the numbers. Rounding (for example "about 12k") is already applied by the tool.
- Never add colleagues' names or user IDs. Other people's usage is only ever "other project
  members".
- If the user wants different wording, rewrite the prose but keep every figure as printed.
- While energy parameters are unverified the markdown says "do not publish the energy and
  carbon figures"; keep that warning.

## Verification

Every figure in the final text appears unchanged in the `markdown` field.
