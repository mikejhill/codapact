---
type: overview
status: active
authority: informative
criticality: low
review:
  status: unreviewed
---

# Operations

Release automation publishes versioned artifacts. Asynchronous mutation analysis
creates tracked follow-up debt and does not become an eager merge/release gate.

- [Releasing](releasing.md): versioning, protected publication, and verification.
- [Mutation testing](mutation-testing.md): asynchronous runs, alert classification,
  ownership, and recovery evidence.

Use [development verification](../development/verification.md) to distinguish
completed checks from historical mutation scores and unsupported environments.
