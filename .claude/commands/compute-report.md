# /compute-report — Answer a compute-usage question from the tracker

Turn a free-text question (`$ARGUMENTS`) into `mn5track` calls, run them with `--format json`,
and answer with a table plus a short interpretation. Local database only.

Pick the command:

| Question shape | Command |
|---|---|
| "How much did project X use in total / over its lifetime?" | `uv run mn5track project-lifetime X --format json` (report `coverage` too) |
| "Usage by month / account / project / state / job family / workdir" | `uv run mn5track report --by <dim> --format json` |
| "How much on allocation A" | `uv run mn5track report --account A --format json` |
| "Most expensive jobs / experiments" | `uv run mn5track top --n 20 --format json` (add `--families` for job-name families) |
| "Wasted compute / failures" | `uv run mn5track report --by queue --format json` (production waste vs expected debug failures), then `--by state` |
| "Energy / carbon footprint of X" | `uv run mn5track energy --project X --by month --format json`; project-wide incl. members: `project-lifetime X` → `energy` |
| "Why is job N counted as project P?" | `uv run mn5track explain N --format json` |

Filters combine: `--project`, `--account`, `--from YYYY-MM-DD`, `--to YYYY-MM-DD`,
`--partition acc|gpp`. Valid `--by`: month, account, project, state, jobname-prefix, partition,
workdir, queue. Projects are those in `config/project.yaml`, plus `unattributed`.

Answer rules:
- Every number must come from the JSON output. Do not compute totals the JSON does not contain;
  run another query instead.
- Headline GPU number is `gpu_hours_billed` (what the budget was charged). Mention
  `gpu_hours_node` (full-node view) only when asked or when they differ by more than 5 %.
- Distinguish allocation (Slurm account) from scientific project in every answer.
- Always state the caveats that apply:
  - `report`/`top`: sacct shows **own jobs only**.
  - `project-lifetime`: colleagues' usage is a **lower bound** from bsc_acct snapshots of
    dedicated allocations; list `invisible_allocations` with their reasons.
  - Energy/CO2e: estimates; give the low–high range and `measured_share`, and say "draft"
    while `assumptions.verified` is false.
  - TIMEOUT is reported separately from wasted compute because checkpointed training often
    times out on purpose.
- Colleagues' names never appear in these outputs; do not look them up.
