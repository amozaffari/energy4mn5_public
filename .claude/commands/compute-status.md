# /compute-status — Allocation budgets, expiry and risks

Summarise the state of every MN5 allocation from the local tracker. Reads the local database only;
nothing runs on MN5 (run `/compute-sync` first if the data is stale).

Run from the repo root:

```bash
uv run mn5track alerts --format json
uv run mn5track status --format json
```

`alerts` exits 1 when there is at least one alert; that is expected, not a failure.

Answer in **at most 10 lines**, risks first:
1. Critical alerts: lost visibility, expiry within 7 days, ≥95 % used, no recent sync
2. Warnings: expiry within 30 days, ≥80 % used, projected exhaustion before expiry
3. Then one line per visible budgeted allocation: account, used %, used/budget node-h,
   expiry date, days left
4. Allocations with `visible_in_bsc_acct: false`: say their numbers are the final snapshot and
   give its date (`snapshot_ts`)

Units: `status` node-hours are all users (bsc_acct khours × 1000 / 80 for ACC). Say which
`burn_rate_source` a projection used; "own sacct jobs" projections are a lower bound on burn.
Always say "allocation" (Slurm account), not "project".

Finish with one line pointing to the visual overview: `uv run mn5track dashboard` (add
`--project <name>` for a project view) and the path it prints.
