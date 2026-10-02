---
name: compute-report
description: Answer a free-text question about MareNostrum 5 compute usage (per project, allocation, month, job family, waste, energy and carbon) from the local mn5track database, with every number quoted from JSON output.
version: 1.0.0
author: Amirpasha Mozaffari
platforms: [macos, linux]
metadata:
  hermes:
    category: hpc
    tags: [hpc, marenostrum5, reporting, energy, carbon]
    related_skills: [compute-status, compute-statement, compute-attribute]
---

# compute-report: answer a compute-usage question

Turn the user's question into `mn5track` calls, run them with `--format json`, and answer with
a table plus a short interpretation. Local database only.

## When to Use

Any question about how much compute was used, by whom (in aggregate), where, when, how much
was wasted, or what it cost in energy and carbon.

## Quick Reference

| Question shape | Command |
|---|---|
| Lifetime total of project X | `uv run mn5track project-lifetime X --format json` (report `coverage`) |
| Usage by month / account / project / state / job family / workdir / queue | `uv run mn5track report --by <dim> --format json` |
| Usage on allocation A | `uv run mn5track report --account A --format json` |
| Most expensive jobs or experiments | `uv run mn5track top --n 20 --format json` (`--families` for job-name families) |
| Wasted compute, failures | `uv run mn5track report --by queue --format json`, then `--by state` |
| Energy or carbon of X | `uv run mn5track energy --project X --by month --format json`; project-wide including other members: `project-lifetime X` → `energy` |
| Why job N counts as project P | `uv run mn5track explain N --format json` |

Filters combine: `--project`, `--account`, `--from YYYY-MM-DD`, `--to YYYY-MM-DD`,
`--partition acc|gpp`. Valid `--by`: month, account, project, state, jobname-prefix,
partition, workdir, queue. Projects are those in `config/project.yaml`, plus `unattributed`.

## Procedure

1. Map the question to one or more commands from the table. Run them from the repository root.
2. Build the answer table only from fields present in the JSON.
3. Add a short interpretation and the caveats below that apply.

## Pitfalls

- Every number must come from the JSON output. Do not add up or convert numbers yourself; run
  another query instead.
- The headline GPU number is `gpu_hours_billed` (what the budget was charged). Mention
  `gpu_hours_node` only when asked or when the two differ by more than 5 %.
- Distinguish allocation (Slurm account) from scientific project in every answer.
- Caveats to state:
  - `report` and `top`: sacct shows **own jobs only**.
  - `project-lifetime`: other members' usage is a **lower bound** from bsc_acct snapshots of
    dedicated allocations; list `invisible_allocations` with their reasons.
  - Energy and CO₂e are estimates: give the low–high range and `measured_share`, and say
    "draft" while `assumptions.verified` is false.
  - Wasted compute counts production failures only. Failures in debug or interactive queues
    are "failed in debug (expected)"; quote `production_success_rate` as the success rate.
  - TIMEOUT is reported separately from waste, because checkpointed training often times out
    on purpose.
- Colleagues' names never appear in these outputs; do not look them up.

## Verification

Re-read each figure in the answer against the JSON it came from before replying.
