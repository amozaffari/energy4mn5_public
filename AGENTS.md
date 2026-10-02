# mn5-compute-tracker — Agent Guidelines

## Overview

`mn5track` records, attributes and reports compute use on MareNostrum 5 (MN5, BSC) across
allocations. MN5 only shows a user their own jobs (`sacct`), and colleagues' usage (`bsc_acct`)
only while the user is still in the allocation's Unix group. The tracker therefore snapshots
regularly into a local SQLite database and keeps that history after access is lost.

Key paths:

- **Package:** `src/mn5_tracker/` (entry point `cli.py`, command `uv run mn5track`)
- **Config:** `config/project.yaml` (shared privately, gitignored: node specs, allocations,
  projects, attribution rules) + `config/tracker.yaml` (personal, gitignored: username, ssh
  hosts). Tests use `config/project.example.yaml`; never copy real names into it.
- **Database:** `~/.local/share/mn5-tracker/tracker.db` (override with `MN5TRACK_DB`); raw
  gzipped command outputs live next to it under `raw/<date>/`
- **Skills:** `.claude/commands/compute-*.md`
- **Tests:** `tests/`, fixtures captured from MN5 and sanitised in `tests/fixtures/`

## Rules for agents

1. **Read-only on MN5.** The tracker never runs `sbatch`, `scancel`, `srun`, `scontrol update`,
   `sacctmgr add/modify/delete` or anything that writes on MN5. `remote.py` enforces an
   allowlist; do not bypass it, and do not ssh to MN5 yourself for tracker questions.
2. **Never guess numbers.** Every figure in an answer must come from a `mn5track ... --format
   json` output. If a number is missing, run another query rather than computing it by hand.
3. **Allocation ≠ project.** An allocation is a Slurm account (`bsc32`, `ehpc103`); a project
   is a scientific project (`geofm`). Name which one you mean in every answer.
4. **Privacy.** Colleagues' names from `bsc_acct` may appear in local tables, but never in
   generated statements or anything meant for publication. Aggregate them as "other project
   members". For a page to share outside the project, use `dashboard --anonymise`.
5. **Waste vs debug:** "wasted compute" means failures in production QOS only; failures in
   `queues.development_qos` (debug/interactive) are "failed in debug (expected)". Quote
   `production_success_rate` as the success rate, and give the debug figure separately.
6. **State the caveats:** sacct is own-jobs-only; `project-lifetime` is a lower bound (report its
   `coverage`); allocations never snapshotted are invisible; TIMEOUT is not counted as waste.
7. Only edit `config/project.yaml` or `config/tracker.yaml` after the user confirms the diff.
8. **Contribution files** (`contributions/`, gitignored) contain a member's username and usage.
   Never commit them to a public repository, and never paste their contents into statements.

## Units

| Quantity | Definition |
| --- | --- |
| `node_hours` | ElapsedRaw × NNodes / 3600: nodes occupied |
| `billed_node_hours` | billing TRES × ElapsedRaw / hw_threads per node: what the budget pays |
| `gpu_hours_billed` | billed node-h × 4 on ACC. **Headline GPU number** |
| `gpu_hours_node` | node-h × 4 on ACC (full-node view) |
| `gpu_hours_requested` | `gres/gpu` × hours |
| bsc_acct khours | thousand physical-core-hours; ACC node-h = khours × 1000 / 80 |

Energy/CO2e (`energy.py`) are **estimates**: measured IPMI energy only for exclusive GPP jobs,
modelled otherwise (ACC always). Always give the range and the measured share, and say "draft" while
`energy.verified` is false in `config/project.yaml`.

sacct billing counts hardware threads (160 per ACC node, 224 per GPP node). A 1-GPU ACC job is
billed a quarter node (billing=40), not a full node. `sync` reconciles own sacct usage against
the user's own bsc_acct rows and warns when they differ by more than 10 %.

## Commands

| Command | Purpose |
| --- | --- |
| `sync [--full] [--since D]` | Pull sacct, snapshot bsc_acct/associations/groups/quota |
| `status` | Budget, used %, expiry, burn projection, visibility per allocation |
| `alerts` | Exit 1 with a list of budget/expiry/exhaustion/visibility/stale alerts |
| `report [--by ...] [filters]` | Aggregated usage, success rate, waste, timeouts |
| `project-lifetime P` | Lifetime usage: own + contributions + bsc_acct fallback per person |
| `top [--families]` | Most expensive jobs or job-name families |
| `explain JOBID` | All stored fields plus the attribution rule that matched |
| `statement P` | Markdown paragraph and table for papers and proposals |
| `energy [--by ...] [filters]` | Estimated MWh and t CO2e (low/central/high), measured share |
| `dashboard [--project P \| --all]` | Self-contained HTML overview; `--all` writes every view to `dashboards.out_dir` |
| `dashboard --anonymise` | Same page with aliases for projects, allocations and people (for sharing) |
| `chart monthly\|burndown` | PNG + HTML charts |
| `export P` / `import FILES` | Share own aggregated usage / import other members' files |
| `contributors [--project P]` | Imported contributions, freshness, rules match |
| `reparse` | Rebuild tables from the stored raw outputs |
| `schedule install\|uninstall\|show` | Daily launchd sync on the Mac |

Every command accepts `--format json` (stable schemas named `mn5track.<command>/v1`).

## Tools

| Tool | Purpose |
| --- | --- |
| `uv` | Package manager: `uv add` / `uv remove`, never `pip install` |
| `uv run ruff check --fix` / `uv run ruff format` | Lint and format |
| `uv run ty check` | Type check |
| `uv run pytest` | Tests (no network; ssh is mocked) |

## Coding Style

Self-explanatory code: structure and naming over comments.

- **Comments:** only docstrings, "why" notes for non-obvious design decisions, and genuinely
  complex logic.
- **Naming:** descriptive names; singular nouns in loops (`for job in jobs`). Single letters only
  for matrix/array indices.
- **Whitespace:** blank lines between logical blocks, and before a nested control structure that
  follows other statements.
- **Flat control flow:** early returns, `continue` and guard clauses instead of deep nesting.
- **Typing:** annotate every function parameter, return type and class attribute; local
  variables are inferred.
- **Architecture:** a thin `main`/command function that orchestrates descriptively named helpers.
  Parsers live in `collectors/`, rendering in `reports/`, and they never talk to each other
  directly. `sync.py` wires collectors to `store.py`.
- **Fail loudly:** parsers raise `ParseError` on any line they do not recognise; a sync records
  the error and keeps the other collectors' data. Raw outputs are always stored first, so a
  parser fix plus `mn5track reparse` recovers the data.

## Contribution Steps

1. Run `uv sync` after any dependency change.
2. Before committing: `uv run ruff check --fix`, `uv run ruff format`, `uv run ty check`,
   `uv run pytest`. The pre-commit hook runs ruff.
3. New MN5 output shapes: capture a real sample, sanitise colleagues' names and IDs, add it to
   `tests/fixtures/`, and add a parser test.
4. Keep `AGENTS.md` and `README.md` up to date with structural changes. Keep this file under
   ~150 lines.
