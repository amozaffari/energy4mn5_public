# /compute-sync — Snapshot MN5 compute usage into the local tracker

Pull new Slurm jobs and snapshot `bsc_acct`, associations, groups and quota from MareNostrum 5
into the local tracker database. Read-only on MN5.

**Remote host:** MN5G1 (fallback: MN5ACC1)

**Host fallback:** `mn5track sync` tries `MN5G1` (glogin1) first. If it does not connect within
15 s (timeout, `Connection refused`, or the command hangs), it repeats the same commands on
`MN5ACC1` (alogin1). Both login nodes see the same GPFS and Slurm database, so nothing else
changes. Tell the user which host was used (the `host` field).

It always passes `ssh -o ConnectTimeout=15 -o BatchMode=yes`. Do not run ssh yourself for this.

Parse `$ARGUMENTS`:
- `--full`: re-read the whole sacct history (use after changing `node_types` or on first run)
- `--since YYYY-MM-DD`: explicit window start
- Default: incremental (last max Submit − 7 days)

Run from the repo root:

```bash
uv run mn5track sync --format json $ARGUMENTS
```

Exit code 1 means at least one collector failed; the other collectors' data is still stored.

Report, in this order:
1. Host used, and any `failed_attempts` (primary host down)
2. Any `result.visibility_changes` with `after: false` — an allocation disappeared from Slurm
   associations, `id -Gn` or bsc_acct. Say that its last snapshot is now its permanent record
3. `result.errors` and `result.warnings` verbatim (node-spec or reconciliation warnings mean
   the node specs in `config/project.yaml` may be wrong)
4. `jobs_new`, `jobs_updated`, and up to 10 of `new_jobs` (id, name, account)
5. Snapshot row counts (budget, user, association, quota)

Never invent numbers; quote them from the JSON.
