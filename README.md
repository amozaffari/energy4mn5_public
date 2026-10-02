# mn5-compute-tracker

Record, attribute and report HPC compute use on MareNostrum 5 (MN5, BSC) across allocations, for
one person or a whole project team.

It answers questions like these:

- How many GPU-hours has GeoFM used over its lifetime, across every allocation it ran on?
- How much budget is left on each allocation, and when does it expire or run out?
- How is consumption trending by month? Which jobs or job families cost the most?
- How much compute went into failed, cancelled or timed-out jobs?
- What goes in the "computing resources" paragraph of a paper or proposal?

## Why a tracker is needed

MN5 doesn't delete accounting data, but it limits what each user can see:

| Source | Shows | How long you can see it |
| --- | --- | --- |
| `sacct` | only **your own** jobs (`PrivateData` hides everyone else's) | forever, even after leaving an allocation |
| `bsc_acct` | **everyone's** usage, the budget and the expiry date | only while you are in the allocation's Unix group |

`bsc_acct` has no arguments. It prints one block for each Unix group you belong to **today**,
and it only shows the current cumulative total, with no dates. So:

- when you are removed from an allocation, its budget and your colleagues' usage disappear from
  view for good;
- even while you can see it, you can't ask what the value was last month.

The tracker takes a dated copy of everything every day and keeps it locally. When an allocation
vanishes, the last copy becomes its permanent record.

## How it works

```
 your Mac (daily, launchd)                      MN5 login node (read-only)
 ─────────────────────────                      ──────────────────────────
 mn5track sync ──── one ssh session ──────────► sacct         your jobs
    │               MN5G1, fallback MN5ACC1     bsc_acct      all users, budgets
    │                                           sacctmgr      accounts you can use
    │  ◄──── output wrapped in markers ──────── id -Gn        your Unix groups
    │                                           bsc_quota     storage
    ▼
 1. save raw output (gzip)   ~/.local/share/mn5-tracker/raw/<date>/
 2. parse                    collectors/*.py: fail loudly on any unknown line
 3. store (SQLite)           jobs: one row per job, frozen once finished
                             *_snapshots: appended every sync, never overwritten
 4. self-check               sacct billing vs bsc_acct; visibility changes → alerts
    │
    ▼
 status · alerts · report · top · project-lifetime · statement · chart
 (read the local database only; no network)
```

1. **Collect.** One ssh session runs all five commands. Each is wrapped in
   `__MN5TRACK_BEGIN/END__` markers, so the login shell's `load impi/...` noise is ignored. If
   glogin1 doesn't answer within 15 s, the same batch runs on alogin1. An allowlist refuses
   anything that isn't a read-only command, so the tracker can never submit or cancel jobs.
2. **Keep the raw output first.** If MN5 changes a format and a parser breaks, nothing is lost.
   Fix the parser and run `mn5track reparse` to replay every stored run.
3. **Store.** Running jobs are updated on later syncs, and finished jobs are frozen. Incremental
   syncs re-read the last 7 days. Snapshots are append-only.
4. **Check itself.** Each sync compares your own sacct billing with your own row in bsc_acct.
   They usually agree within 0.1 %, and a gap over 10 % means the node specs in the config are
   wrong. It also compares today's visibility with yesterday's. If an allocation disappears from
   `id -Gn`, sacctmgr or bsc_acct, that's a critical alert.

### From jobs to answers

- **Units.** Every job becomes billed node-hours per partition: `billing × hours / 160` on ACC,
  `/ 224` on GPP. GPU-h = billed ACC node-h × 4. bsc_acct khours convert as node-h =
  khours × 1000 / 80. See [Units](#units).
- **Attribution.** An *allocation* (Slurm account, who paid) is not a *project* (what science it
  was): `bsc32` funds many unrelated projects, and GeoFM ran on seven allocations. Ordered
  rules in `config/project.yaml` map each job to a project. They match on WorkDir, job name,
  account and submit date; the first match wins, and anything else is `unattributed`. Rules are
  re-applied every time a report runs. `explain <jobid>` shows which rule matched, and
  `/compute-attribute` proposes new rules.
- **Lifetime totals** combine sources per person and per allocation. For each allocation:

  ```
  project total  =  your own jobs (sacct)
                 +  contributions from other members (their own sacct, see below)
                 +  bsc_acct rows of members who have not contributed (dedicated allocations only)
  ```

  An allocation that lapsed before tracking started has no bsc_acct snapshot, so only members
  with detailed data count there. The result is therefore always labelled a lower bound, with
  its coverage stated.

## Energy and carbon footprint

`mn5track energy` estimates the electricity and CO₂e behind the billed node-hours. It follows
the Green Algorithms approach (Lannelongue et al., 2021): energy = power × time × PUE, and
CO₂e = energy × grid carbon intensity.

```
billed node-h per job (own jobs, contributions, bsc_acct fallbacks)
   ├─ GPP: measured IPMI energy (exclusive full-node jobs)  or  calibrated W/node × node-h
   └─ ACC: (4 × GPU power × utilisation + host power) × node-h     → low / central / high
IT energy × PUE = facility energy  →  × grid intensity of the month = kg CO₂e (location-based)
```

What MN5 records:

- **GPP.** Slurm stores IPMI energy per **whole node**, sampled every 60 s. Readings are used
  only for exclusive full-node jobs, which draw a consistent ~660 W per node. A job sharing a
  node reports everyone's energy (2–18× too high per billed node-hour), so shared-node jobs are
  modelled with the power calibrated from the exclusive ones.
- **ACC.** No energy or GPU utilisation is recorded (`energy=0`), so ACC is modelled. Three GPU
  utilisation scenarios give the range. CodeCarbon measurements from real runs can replace the
  assumption (phase 2, planned).

Every parameter lives in `config/project.yaml` under `energy:`: PUE, GPU and host power,
utilisation scenarios, grid intensity (a default, or per year or month), and optional
market-based intensity and embodied carbon. Each carries a source note. Until you set
`energy.verified: true`, every output says the figures are a draft, and `statement` adds
"do not publish".

```bash
uv run mn5track energy --by project          # own jobs: MWh and t CO₂e with ranges
uv run mn5track energy --project geofm --by month
uv run mn5track project-lifetime geofm     # JSON `energy`: all members, with assumptions
uv run mn5track statement geofm            # adds the energy/CO₂e sentence
```

The dashboard shows energy and carbon tiles and a monthly carbon chart split into ACC and GPP.

## Several members, one project total

Everyone can only see their own jobs, but everyone keeps them forever. So the project total is
rebuilt from every member's share:

```
 member A: sync → export geofm ─┐
 member B: sync → export geofm ─┼──► private channel ──► import contributions/
 member C: sync → export geofm ─┘    (private repo,       project-lifetime geofm
                                        GPFS project dir)    statement geofm
```

```bash
uv run mn5track export geofm              # → contributions/geofm/<user>.json (gitignored)
uv run mn5track import contributions/       # other members' files; newest per member wins
uv run mn5track contributors                # who contributed, how fresh, same rules?
uv run mn5track project-lifetime geofm    # now combines every contributor
```

- **Contents.** A contribution holds totals per month × allocation × partition × state, plus
  the member's username. It has no job names, paths or job IDs. Share it through a private
  channel, never a public repository.
- **No double counting.** On each allocation, a member whose own sacct covers that allocation
  is counted from their file, and their bsc_acct row is dropped. Everyone else keeps their
  bsc_acct figure, so an incomplete file can never make usage disappear.
- **Filling the gaps.** Contributions fill what bsc_acct can't cover: allocations that lapsed
  before tracking started, and members' project work on shared allocations such as `bsc32`.
- **Checks on import.** It warns when a file was made with different attribution rules, or when
  it disagrees with that member's bsc_acct row by more than 10 %.
- **Statements name no one.** `statement` states coverage ("Slurm accounting of N project
  members, plus BSC accounting totals for up to M others") without names.

## Install

```bash
uv sync
uv run mn5track --help
```

You need ssh host aliases `MN5G1` (primary) and `MN5ACC1` (fallback) in `~/.ssh/config`, with
key-based login (`BatchMode=yes`, so there is never a password prompt). Nothing is installed or
run on MN5 beyond the stock `sacct`, `sacctmgr`, `bsc_acct`, `bsc_quota` and `id` commands.

## Configuration

| File | In git? | Holds |
| --- | --- | --- |
| `config/project.yaml` | **no**, shared privately between members | node specs, allocations, projects, attribution rules |
| `config/tracker.yaml` | **no**, personal | your MN5 username, ssh hosts, optional `extra_attribution_rules` |
| `config/project.example.yaml` | yes | placeholder allocations and projects; the tests use it |

`project.yaml` names real allocations, projects and directory layouts, so it stays out of git.
Start from the example, then pass the file to other members through a private channel (a
private repo or the project's GPFS directory). Everyone should use the same file, so that
contributions are attributed with the same rules. `project_config:` in `tracker.yaml` can
point to it anywhere.

Personal keys override shared ones. `extra_attribution_rules` are checked before the shared rules,
for members whose directory layout differs.

## First run

```bash
cp config/tracker.example.yaml config/tracker.yaml    # then set your MN5 username
cp config/project.example.yaml config/project.yaml    # or the project's shared copy
uv run mn5track sync --full                            # whole sacct history + first snapshot
uv run mn5track status
uv run mn5track alerts
uv run mn5track report --by project
```

## Daily snapshots

Run `sync` daily from your laptop, never from MN5:

```bash
uv run mn5track schedule install --hour 7     # macOS launchd agent; `show` prints the plist
```

The agent runs `sync`, regenerates the dashboards (`dashboard --all`), then runs `alerts`. It shows a macOS notification if either reports a problem,
and logs to `~/Library/Logs/mn5track.log`. A Claude Code routine that runs `/compute-sync` then
`/compute-status` also works. What matters is snapshotting often enough to capture an
allocation's final numbers before membership is revoked.

## Commands

| Command | What it does |
| --- | --- |
| `sync [--full] [--since YYYY-MM-DD]` | Incremental sacct pull (last Submit − 7 days) plus snapshots |
| `status` | Budget, used, % used, expiry, burn-rate projection, visibility |
| `alerts` | Exit 1 when: ≥80 % used, expiring within 30 days, projected to run out, vanished, stale sync |
| `report --by month\|account\|project\|state\|queue\|jobname-prefix\|partition\|workdir` | Aggregated usage, success rate, waste, timeouts |
| `top [--n 20] [--families]` | Most expensive jobs or job-name families |
| `explain <jobid>` | Every stored field, and which attribution rule matched |
| `project-lifetime <project>` | Own + contributed + bsc_acct usage per allocation, with coverage |
| `statement <project>` | Markdown paragraph and table for papers and proposals |
| `export <project>` / `import <files>` | Share own aggregated usage / import other members' files |
| `contributors [--project P]` | Imported contributions, freshness, rules match |
| `energy [--by ...] [filters]` | Estimated energy (MWh) and carbon (t CO₂e) with low–high ranges |
| `dashboard [--project P \| --all] [--anonymise] [--out PATH]` | One-page overall-usage dashboard (self-contained HTML) |
| `chart monthly\|burndown [--out charts/]` | PNG + self-contained HTML |
| `reparse` | Rebuild the database from the stored raw outputs |
| `schedule install\|uninstall\|show` | Daily launchd sync on macOS |

Filters: `--project`, `--account`, `--from`, `--to`, `--partition acc|gpp`. Output:
`--format table|md|csv|json`. JSON schemas are versioned (`mn5track.<command>/vN`).

### Dashboard

`mn5track dashboard` writes one self-contained HTML page (no internet needed) with:

- **Headline tiles**: billed GPU-hours, ACC and GPP node-hours, success rate, share of wasted
  compute, active allocations.
- **Usage over time**: billed node-hours per month, stacked by project, allocation or queue
  (production vs development), with a toggle.
- **Share**: lifetime billed node-hours by project, or by allocation in a project view.
- **Outcomes**: monthly split into completed, timeout, failed in production, failed in debug
  (expected) and running. Success rate and wasted compute count production queues only.
- **Allocations**: used vs budget per allocation, with the expiry date and bsc_acct points.
- **Energy and carbon**: tiles with ranges; monthly kg CO₂e split into ACC and GPP; carbon by
  project (or by allocation in a project view); carbon by queue (production vs development);
  measured vs modelled energy; and an assumptions
  table listing every parameter with its source and verification status.
- **Health strip**: last successful sync and every active alert.

Hover any bar or point for exact values; every chart has a "Show table" view, and the page follows
the system light/dark setting. `--project geofm` switches to the project view: your jobs,
imported contributions and bsc_acct totals, with coverage stated, the same numbers as
`project-lifetime`. The default output is `~/.local/share/mn5-tracker/dashboard.html` (or
`dashboard-<project>.html`).

`dashboard --all` writes your own view plus every project listed in the personal
`tracker.yaml`, into a folder you choose:

```yaml
dashboards:
  out_dir: "~/Library/CloudStorage/GoogleDrive-<you>/My Drive/mn5-tracker"
  projects: [geofm]
```

The daily launchd job runs `dashboard --all` after each sync. If `out_dir` is a Google Drive or
Dropbox folder, the latest pages are on every device. Drive shows HTML as source text, so download
the file or open it with a browser to see the charts.

### Anonymised dashboards for sharing

`--anonymise` (with `--project`, `--all` or on its own) writes the same page with every name
that ties it to a person, project or allocation replaced by an alias: projects become
"Project A, B, ...", allocations "Allocation 1, 2, ..." (in config order, so the aliases match
across views), you become "Anonymous user" and contributors "Member 1, 2, ...". Allocation titles
are dropped. Hours, dates, outcomes and energy figures stay real. The files are named
`dashboard-anonymised.html` and `dashboard-project-a-anonymised.html`.

Before writing, the tracker searches the output for every original name and any Slurm account or
MN5 username pattern, and refuses to write if one is left. Budgets and expiry dates are kept, so
someone who knows the public EuroHPC award lists could still match an allocation by its size and
end date.

### Charts

- `chart monthly`: billed node-hours per month, stacked by allocation. Each allocation keeps its
  colour across charts. `--metric gpu_hours_billed|node_hours|khours` switches the measure.
- `chart burndown`: one panel per budgeted allocation. It shows cumulative own usage from sacct,
  all-users points from bsc_acct snapshots, the budget line and the expiry date.

Each chart is written as a PNG plus an HTML page that embeds the image and the same numbers as a
table.

## Debug vs production queues

Failing in a debug queue is part of development, so it isn't counted as waste.
`config/project.yaml` lists the development QOS:

```yaml
queues:
  development_qos: [acc_debug, gp_debug, acc_interactive, gp_interactive]
```

- **Wasted compute** = failed, cancelled or OOM jobs in *production* QOS (e.g. `acc_ehpc`,
  `acc_bsces`, `gp_bsces`).
- **Failed in debug (expected)** is reported separately, so you can still see how much
  development costs.
- **Success rate (production)** = completed / finished production jobs. The overall rate
  including debug runs is still in the JSON (`success_rate`).
- `report --by queue` splits everything into `production` and `development`. Changing the list
  takes effect immediately, because queue kind is derived when reports run, like attribution.
- TIMEOUT stays separate in both, since checkpointed training often times out on purpose.

## Units

| Quantity | Definition |
| --- | --- |
| Node-h (occupied) | ElapsedRaw × NNodes |
| Node-h (billed) | sacct `billing` TRES × hours / hardware threads per node (160 ACC, 224 GPP) |
| GPU-h (billed) | billed ACC node-h × 4. **The headline figure** |
| GPU-h (full node) | occupied node-h × 4 |
| khours (bsc_acct) | thousand physical-core-hours; ACC node-h = khours × 1000 / 80 |

On ACC, a job that asks for 1 GPU is billed a **quarter node** (billing=40), not a full node, so
full-node GPU-h overstates what was charged for those jobs. Both figures are reported. sacct
shows `ConsumedEnergyRaw` only for GPP jobs.

## Data and privacy

| What | Where | In git? |
| --- | --- | --- |
| Database and raw outputs | `~/.local/share/mn5-tracker/` (override with `MN5TRACK_DB`) | no, outside the repo |
| Personal config | `config/tracker.yaml` | no |
| Contribution files | `contributions/` | no |
| Personal Claude settings and `hpc-*` commands | `.claude/settings*.json`, `.claude/commands/hpc-*.md` | no |
| Shared config, code, `compute-*` skills | repo | yes |

- Snapshots are append-only. When an allocation disappears from `bsc_acct`, its last snapshot is
  reported as its final state, with the date it was taken.
- Colleagues' names from `bsc_acct` stay in the local database. Statements only ever say "other
  project members".

## Claude Code skills

| Skill | Does |
| --- | --- |
| `/compute-sync` | Sync and report new jobs, snapshots, visibility changes |
| `/compute-status` | Risks first: expiry, exhaustion, lost visibility; then budgets |
| `/compute-report <question>` | Turns a question into `mn5track` queries and answers with caveats |
| `/compute-statement <project>` | Paper or proposal paragraph |
| `/compute-attribute` | Proposes rules for unattributed jobs; edits config only after you confirm |
| `/compute-share export\|import` | Exchange contribution files with project members |

Agents follow the rules in [`AGENTS.md`](AGENTS.md): read-only on MN5, every number from JSON
output, allocation vs project always explicit, no names in publishable text.

## Project layout

```
config/            project.example.yaml, tracker.example.yaml (templates; real files gitignored)
src/mn5_tracker/
  remote.py        ssh runner: host fallback, sentinel markers, read-only allowlist
  collectors/      parsers: sacct, bsc_acct, associations, quota
  sync.py          collect → raw → parse → store, plus self-checks; reparse
  store.py         SQLite schema and queries
  metrics.py       unit conversions
  energy.py        energy and carbon model (measured + modelled, scenarios, PUE, grid)
  attribution.py   rule engine: job → project
  aggregation.py   reports, top, project lifetime (per-person combination)
  contributions.py export/import of member contributions
  allocation_status.py, alerts.py   budget state, burn projection, alerts
  reports/         tables (rich/md/csv/json), statement, charts, dashboard (+ HTML template)
  cli.py           `mn5track` entry point
tests/             unit and integration tests; sanitised MN5 fixtures
```

## Status and roadmap

**Done:** sync with host fallback and raw storage, append-only snapshots, attribution rules,
reports, alerts, statements, charts, the overall-usage dashboard, the energy and carbon model,
launchd scheduling, and multi-member contributions.
Validated against MN5 in September 2026: job counts and node-hours matched hand counts,
and own sacct billing reconciled with bsc_acct to about 0.1 %.

**Next:**
- CodeCarbon bridge (phase 2): import `emissions.csv` from training runs, match each run to its
  Slurm job, use the measured GPU energy, and fit the ACC utilisation factor for all other jobs.
- Verified energy parameters: MN5's PUE and BSC's electricity contract, and a monthly Spanish
  grid-intensity series.
- Monthly `report` including imported contributions (currently only in `project-lifetime`).
- Optional MCP server exposing `sync`, `status`, `report`, `project_lifetime` and `alerts` as
  tools, backed by the same library functions.

## Development

```bash
uv run pytest
uv run ruff check && uv run ruff format --check && uv run ty check
uv run pre-commit install
```
