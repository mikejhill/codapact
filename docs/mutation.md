# Asynchronous mutation testing

[Mutation workflow](https://github.com/mikejhill/codapact/actions/workflows/mutation.yml)
· [Alert delivery](https://github.com/mikejhill/codapact/actions/workflows/mutation-alerts.yml)
· [Open mutation alerts](https://github.com/mikejhill/codapact/issues?q=is%3Aissue+is%3Aopen+%22%5Bmutation%5D%22)

Mutation testing runs after pushes to `main`, every Monday at 06:23 UTC, and on
manual dispatch. It never runs as a PR check, is not part of `CI passed`, and is
not a merge or release prerequisite. This is a retained project setup preference.
Other formatting, lint, typing, test, coverage, and package checks remain gates.

The Linux job runs mutmut with two workers and a thirty-minute process budget.
The normal suite includes fresh package installation; mutation selection omits
that expensive distribution test while retaining the runtime/generation tests.
Runs start clean; source, test, dependency, and configuration changes cannot reuse
stale cached mutation verdicts. Evidence is retained as a thirty-day artifact.
The artifact includes named mutant verdicts and an archive of generated source
and metadata for inspection. Survivor details are retained even when the complete
score passes. Missing required evidence is an execution failure, not a pass. The bounded
budget was increased after the manifest slice reached 3,845 mutants: an actual
eighteen-minute run completed only 3,271. Worker count remains two; the job has a
forty-minute outer limit so it can preserve evidence after a process timeout.

## Durable alerts

A separate workflow watches completion of trusted same-repository main-branch
mutation runs. It can report even when the scan job fails before producing an
artifact. It has read access to the evidence and issue-write access; the scanning
job has no issue-write permission. Neither workflow runs untrusted PR code with
write permission. Superseded commit results cannot resolve or change current-main
incidents.

Each active class has one deduplicated issue assigned to `mikejhill`:

- **Regression:** a complete score below the required 90% floor. Equality and
  scores above the floor pass even with survivors; survivor details remain in
  evidence without creating a findings issue.
- **Execution failure:** incomplete and infrastructure results share a separate
  incident. Incomplete means zero mutants, unresolved counts, interruptions,
  timeouts, suspicious outcomes, skipped mutants, or crashed workers.
  Infrastructure means tool/baseline/setup failure, missing or malformed evidence.
  The subtype remains explicit in the report and issue. Neither subtype passes.
  The producer workflow conclusion is checked even when
  its artifact is valid: cancelled, timed-out, skipped, and unknown conclusions
  override artifact claims as incomplete. A failed producer cannot establish a
  passing recovery. A complete below-floor report is still valid regression
  evidence when its producer fails because the score is below the floor.

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

## Bounded first triage

The complete run at commit `d9679f3622f0e14caa3c0dfb65c8fac7a654dced`
reported 1,822 killed and 941 surviving mutants (65.94%). This is historical
evidence for that revision, not a score for the later manifest/runtime changes.
The manifest run `40808278696` timed out after 3,271 of 3,845 mutants and has no
complete score. Neither result establishes a current-main passing score.

A bounded review selected network and portable-path boundaries. Regression
tests now cover username-only URLs, port zero, control/DEL characters, raw Unicode
queries, query-free base URLs, mixed public/private DNS answers, empty DNS,
public plaintext HTTP, socket/connection timeout propagation, root request paths,
and trailing-dot/reserved output names. These were assertion gaps; the original
production behavior already rejected or handled the cases correctly.

Some survivors remove a guard already covered by another guard. For example,
`project.x__safe_path__mutmut_6` weakens the backslash/colon predicate while the
path allowlist still rejects both characters. No threshold or exclusion is
changed to hide these survivors. New tests are not claimed to improve the full
mutation score until a complete comparable asynchronous run measures it.

Separately, entry-point tests reproduced an alert recovery defect: a valid
passing artifact could close incidents after a cancelled producer. Producer
conclusion now overrides such artifact claims. Tests invoke production `main()`
with an in-memory GitHub adapter and assert incident states; they perform no
network writes and create no synthetic production alerts.
