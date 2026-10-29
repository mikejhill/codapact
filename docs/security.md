# Security analysis

The CodeQL workflow analyzes Python and GitHub Actions with the
`security-and-quality` queries. It runs on PRs, main pushes, Monday at 06:23 UTC,
and manual dispatch. Actions are pinned to the same immutable revisions used by
VerdiClip's current workflow. No project credentials are required.

The workflow grants `contents: read` globally. Only its analysis jobs receive
`security-events: write` to upload findings. Checkout does not persist credentials;
there is no privileged PR trigger or untrusted artifact execution. CodeQL
concurrency is separate from mutation testing and cannot cancel a mutation run.

Expected job names are `Analyze (python)` and `Analyze (actions)`. The code-scanning
tool name is verified from successful uploaded analyses before configuring a
merge rule. A successful workflow means analysis completed; alert counts must be
inspected separately. No broad query exclusions or severity suppression are used.

Repository rules are a separate persistent setting. The proposed standard is PRs
for all contributors, `CI passed` from GitHub Actions, linear history, no
force-push/deletion, resolved review threads, and no mandatory approving review.
The owner may bypass the CI rule through PRs only. A CodeQL all-alerts rule has no
bypass and is activated only after successful setup and explicit approval.
Installing this workflow does not configure rulesets or default-setup settings.

Mutation remains asynchronous and is never a required merge or release check.
See [mutation testing](mutation.md) for score evidence, execution incidents, and
triage ownership. Credentials should never be committed; runtime credential
storage guidance is in [configuration](configuration.md).
