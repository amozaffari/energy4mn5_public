# /compute-share — Exchange project usage with other members

Each member can only see their own MN5 jobs, so a project's full total is rebuilt from every
member's contribution file. A file holds aggregated totals (month × allocation × partition ×
state) plus the member's username: no job names, paths or job IDs. Local only; nothing runs on MN5.

Parse `$ARGUMENTS`:
- `export <project>` (default when only a project is given): write your contribution
- `import <path>`: import one file or a directory of files
- no arguments: show which contributions are already imported

**Export:**
```bash
uv run mn5track sync            # so the export includes your latest jobs
uv run mn5track export <project>
```
Writes `contributions/<project>/<user>.json` (gitignored). Tell the user the path and remind them
to share it only through a private channel: a private repo, the project's GPFS directory, or
direct message. Never commit it to a public repository.

**Import:**
```bash
uv run mn5track import <path> --format json
uv run mn5track contributors --format json
```

Report:
1. Per file: member, project, status (`imported`, `skipped: own`, `skipped: not newer`,
   `invalid`)
2. Every warning verbatim. "different rules" means that member must pull the latest
   `config/project.yaml` and re-export. A khours mismatch against bsc_acct usually means the
   file is older than our snapshot.
3. Then run `uv run mn5track project-lifetime <project> --format json` and give the new
   `lower_bound_gpu_hours_billed` and `coverage` (members with detailed data vs bsc_acct only).

Members' usernames may appear in this local summary, never in `statement` output.
