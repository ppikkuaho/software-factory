---
instruction_version: 2
calibration_status: calibrated
---

# Plan-alignment whole-portfolio coherence

This instruction is jointly owner+director calibrated. Production dispatch verifies its
notary-backed calibration record for this exact version before opening a seat.

You receive construction artifacts and their harness-authored module map. You do not receive
intent, verification criteria, reconstructions, or a comparator report.

Scan the whole supplied construction portfolio for assumptions that cross module boundaries:
ownership, lifecycle, ordering, identity, retries, error semantics, persistence, units, schemas,
and boundary responsibilities. Record a contradiction only when two modules rely on incompatible
interpretations of the same shared assumption. The finding identity is the module pair plus the
assumption—not a shared requirement ID. Requirement prefixes, when known, are routing metadata.

A contradiction requires that both modules cannot be simultaneously correct. Two modules making
different but compatible choices is not a finding.

Do not perform local design review, rewrite modules, or report stylistic inconsistency. Stay at
shared-boundary materiality.

Write exactly:

```json
{
  "schema_version": 1,
  "bundle_sha256": "<cell bundle>",
  "modules_read": ["api", "worker"],
  "shared_assumptions": [
    {
      "assumption_key": "retry-owner",
      "modules": ["api", "worker"],
      "interpretations": {
        "api": "<interpretation>",
        "worker": "<interpretation>"
      },
      "evidence": {
        "api": "<artifact path>",
        "worker": "<artifact path>"
      }
    }
  ],
  "contradictions": [
    {
      "type": "CONTRADICTION",
      "modules": ["api", "worker"],
      "assumption_key": "retry-owner",
      "incompatible_claims": ["<claim A>", "<claim B>"],
      "evidence_paths": ["<path A>", "<path B>"],
      "affected_trace_prefixes": ["R-001"]
    }
  ]
}
```
