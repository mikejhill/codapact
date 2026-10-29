# Asynchronous mutation testing

[Mutation workflow](https://github.com/mikejhill/codapact/actions/workflows/mutation.yml)
· [Alert delivery](https://github.com/mikejhill/codapact/actions/workflows/mutation-alerts.yml)
· [Open mutation alerts](https://github.com/mikejhill/codapact/issues?q=is%3Aissue+is%3Aopen+%22%5Bmutation%5D%22)

Mutation testing runs after pushes to `main`, every Monday at 06:23 UTC, and on
manual dispatch. It never runs as a PR check, is not part of `CI passed`, and is
not a merge or release prerequisite. This is a retained project setup preference.
Other formatting, lint, typing, test, coverage, and package checks remain gates.

The Linux job runs mutmut with two workers and an eighteen-minute process budget.
The normal suite includes fresh package installation; mutation selection omits
that expensive distribution test while retaining the runtime/generation tests.
Runs start clean; source, test, dependency, and configuration changes cannot reuse
stale cached mutation verdicts. Evidence is retained as a thirty-day artifact.

## Durable alerts

A separate workflow watches completion of trusted same-repository main-branch
mutation runs. It can report even when the scan job fails before producing an
artifact. It has read access to the evidence and issue-write access; the scanning
job has no issue-write permission. Neither workflow runs untrusted PR code with
write permission. Superseded commit results cannot resolve or change current-main
incidents.

Each active class has one deduplicated issue assigned to `mikejhill`:

- **Regression:** a complete score below 90%, or mutants without associated tests.
- **Incomplete:** zero mutants, unresolved counts, interruptions, timeouts,
  suspicious outcomes, skipped mutants, or crashed workers. These never pass.
- **Infrastructure:** tool/baseline/setup failure, missing or malformed evidence.

Issues contain first/last seen, commit and run links, recent evidence, the score
and floor, completion state, and triage instructions. Repeated observations update
the issue instead of creating duplicates. A weekly reminder comment re-notifies
the owner. Only a complete current-main result can resolve incidents; incomplete
or infrastructure failures leave known quality regressions open. Closed incidents
remain as history; recurrence reopens the same class incident instead of creating
issue spam.

Run IDs and attempts prevent older evidence from reopening or resolving newer
incidents. Findings only recover under the same mutation scope. Raw counts must
agree with a claimed score; a forged or malformed pass cannot resolve an issue.
Only owner/bot-authored incidents are managed. Alert code always comes from main;
downloaded reports are parsed as data and never executed. These constraints are
the documented reason for the narrow `workflow_run` audit exception.

**Owner: Mike Hill (`mikejhill`).** Triage within one working day. Review open
alerts and the alert-delivery workflow weekly, including when no issue appeared.
Inspect surviving mutations, add meaningful assertions, and rerun on main.
Do not lower the floor to silence alerts. GitHub outages, revoked issue permission,
or a disabled alert workflow cannot deliver issues; the visible workflow links
and weekly review cover that delivery failure boundary.

Manual `Mutation alerts` dispatch runs an isolated hosted lifecycle self-test:
create a clearly marked test incident, update/deduplicate it, and close it on a
synthetic recovery. The self-test namespace cannot touch production incidents.
Local mock tests cover all classes, reminders, recurrence, deduplication, and
recovery. No deliberately broken production code is required.

For a local Linux or WSL run: `uv run --group mutation mutmut run`. Native Windows
does not provide mutmut's required fork support. A partial local experiment is
reported as partial evidence, not a complete project score.
