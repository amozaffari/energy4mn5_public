# /compute-statement — Resource-usage paragraph for a paper or proposal

Produce the "computing resources" paragraph and table for project `$ARGUMENTS` (default:
`geofm`), for a paper's acknowledgements or a proposal's "previous usage" section.

Run from the repo root:

```bash
uv run mn5track statement $ARGUMENTS --format json
```

Show the `markdown` field verbatim, then add at most three lines:
1. The period covered and the date of the newest bsc_acct snapshot it relies on
2. Coverage (`lifetime.coverage`): members with detailed data vs bsc_acct totals only, and
   `lifetime.invisible_allocations`, so the total is a lower bound. Suggest `/compute-share` if
   few members have contributed
3. A reminder that the acknowledgement sentence is a draft; the grant agreement's mandated
   wording wins

Rules:
- Do not edit the numbers. Rounding (e.g. "about 12k") is already applied by the tool.
- Never add colleagues' names or user IDs. Other people's usage is only ever
  "other project members".
- If the user wants different wording, rewrite the prose but keep every figure as printed.
