---
type: reference
status: active
authority: informative
criticality: normal
review:
  status: unreviewed
---

# Documentation convention

The root README is the project entrance. Every directory under `docs/` has a
README with useful primary content and a relative-link index of its direct
children. Create a topic directory only when it contains useful content.

| Directory | Meaning |
| --- | --- |
| `architecture/` | Current structure, boundaries, and security analysis |
| `guides/` | Task-oriented consumer procedures |
| `reference/` | Lookup material and implemented behavioral contracts |
| `development/` | Contributor checks and bounded verification evidence |
| `operations/` | Release and mutation-triage procedures |
| `branding/` | Identity guidance and visual assets |

Reserve `decisions/` for historical decision rationale and `plans/` for exploratory
intent when those records are needed. Neither an accepted decision nor a passing
metadata check proves that its implementation exists.

## Frontmatter profile

Use [the repository schema](../../schemas/documentation.schema.json). This local
convention borrows selected OKF concepts; it does not claim formal OKF adoption.
In particular, `active` is local lifecycle vocabulary, not OKF's `stable`.

| Field | Meaning |
| --- | --- |
| `type` | overview, guide, reference, architecture, runbook, decision, or plan |
| `status` | Document lifecycle; decision and plan vocabularies are distinct |
| `authority` | informative, normative, exploratory, or historical |
| `criticality` | low, normal, or high |
| `review.status` | unreviewed, needs-review, or verified |

All five are required, including on content-bearing READMEs. The `type` key is
also OKF's required ordinary-document field. Omit timestamps and empty optional
objects. Use Git history for edit chronology. Authority is explicit per document;
it is not inferred from its folder or generator.

Optional `generated` records the actual generator, canonical sources, and a
reproducible method. Optional `verified` requires an actual reviewer, evidence,
and bounded scope; use it only with `review.status: verified`. Never invent human
review or infer verification from authorship. High-criticality runbooks remain
draft/needs-review until the operational procedure is verified. Superseded
documents link their replacement. Extra non-temporal profile fields belong under
`extensions`.

The [official OKF specification](https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md)
defines optional provenance and trust families with different shapes. Do not
label the local profile's `generated` and `verified` objects as those wire formats.

## Check and maintain

Run `uv run poe docs` to validate frontmatter, local link targets, and direct-child
README indexes. It also runs inside `uv run poe check`. These structural checks do
not verify prose or operational safety. Review linked explanations when code or
contracts change; preserve historical evidence with its original scope.
