---
name: compute-sync
description: Snapshot MareNostrum 5 compute usage (Slurm jobs, bsc_acct budgets, groups, quota) into the local mn5track database. Read-only on MN5.
version: 1.0.0
author: Amirpasha Mozaffari
platforms: [macos, linux]
metadata:
  hermes:
    category: hpc
    tags: [hpc, marenostrum5, slurm, accounting]
    related_skills: [compute-status, compute-report]
---

# compute-sync: snapshot MN5 usage

Pull new Slurm jobs and snapshot `bsc_acct`, associations, Unix groups and quota from
MareNostrum 5 into the local tracker database. The tracker runs a fixed allowlist of read-only
commands over one ssh session; nothing on MN5 is ever written.

## When to Use

- The user asks to sync, refresh or update the compute tracker.
- Another compute skill reports stale data (`stale_sync` alert, or `last_sync` older than a day).
- First run on a new machine (use `--full`).

## Quick Reference

```bash
uv run mn5track sync --format json            # incremental: last max Submit − 7 days
uv run mn5track sync --full --format json     # whole sacct history (first run, node-spec changes)
uv run mn5track sync --since 2026-01-01 --format json
```

Run every command from the repository root.

## Procedure

1. Pick the options from the user's request: `--full`, `--since YYYY-MM-DD`, or none.
2. Run `uv run mn5track sync --format json` with those options. Do not run ssh yourself: the
   tracker tries host `MN5G1` (glogin1) first and falls back to `MN5ACC1` (alogin1) after 15 s.
3. Report, in this order:
   1. `host` used, and any `failed_attempts` (primary host down).
   2. Any `result.visibility_changes` with `after: false`: an allocation disappeared from
      Slurm associations, `id -Gn` or bsc_acct. Say that its last snapshot is now its
      permanent record.
   3. `result.errors` and `result.warnings` verbatim. Node-spec or reconciliation warnings
      mean the node specs in `config/project.yaml` may be wrong.
   4. `jobs_new`, `jobs_updated`, and up to 10 of `new_jobs` (id, name, account).
   5. Snapshot row counts (budget, user, association, quota).

## Pitfalls

- Exit code 1 means at least one collector failed; the other collectors' data is still stored.
  Report the error, do not retry in a loop.
- ssh needs key-based login (`BatchMode=yes`). A password or passphrase prompt cannot be
  answered from here: tell the user to load their key (`ssh-add`) and retry.
- Never invent numbers; quote them from the JSON.

## Verification

`uv run mn5track status --format json` shows `snapshot_ts` values from the sync just run.
