---
name: compute-status
description: Summarise every MareNostrum 5 allocation from the local mn5track database - budget used, expiry, burn-rate projection, visibility and alerts, risks first.
version: 1.0.0
author: Amirpasha Mozaffari
platforms: [macos, linux]
metadata:
  hermes:
    category: hpc
    tags: [hpc, marenostrum5, budget, alerts]
    related_skills: [compute-sync, compute-report]
---

# compute-status: allocation budgets, expiry and risks

Summarise the state of every MN5 allocation. Reads the local database only; nothing runs on
MN5.

## When to Use

- "How much budget is left?", "When does allocation X expire?", "Are we going to run out?"
- A daily or weekly status message (for example from a scheduled job).

## Quick Reference

```bash
uv run mn5track alerts --format json
uv run mn5track status --format json
```

## Procedure

1. Run both commands from the repository root. `alerts` exits 1 when there is at least one
   alert; that is expected, not a failure.
2. If `alerts` contains a `stale_sync` alert, say so first and suggest `compute-sync`.
3. Answer in **at most 10 lines**, risks first:
   1. Critical alerts: lost visibility, expiry within 7 days, ≥ 95 % used, no recent sync.
   2. Warnings: expiry within 30 days, ≥ 80 % used, projected exhaustion before expiry.
   3. One line per visible budgeted allocation: account, used %, used / budget node-h, expiry
      date, days left.
   4. Allocations with `visible_in_bsc_acct: false`: their numbers are the final snapshot;
      give its date (`snapshot_ts`).
4. Finish with one line pointing to the visual overview: `uv run mn5track dashboard` (add
   `--project <name>` for a project view) and the path it prints.

## Pitfalls

- `status` node-hours are **all users** (bsc_acct khours × 1000 / 80 for ACC), unlike
  `report`, which is own jobs only.
- Say which `burn_rate_source` a projection used; "own sacct jobs" projections are a lower
  bound on burn.
- Always say "allocation" (Slurm account), not "project".
- Every number must come from the JSON output.

## Verification

Each allocation in the answer appears in `status` JSON with the same used % and expiry date.
