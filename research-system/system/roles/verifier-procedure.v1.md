---
title: Seam verifier procedure
version: v1
status: derived
ratification: RQ-8 delegated user edit pass, 2026-07-14
derived_from: system/notes/RESEARCH-SYSTEM-VERIFIER-PROTOCOL-2026-07-07.md
provenance_map:
  VP-01: B4 §1
  VP-02: B4 §2.1, B4 §9
  VP-03: B4 §2.2
  VP-04: B4 §2.3, B4 §4
  VP-05: B4 §2.4, B4 §3, B4 §7.2
  VP-06: B4 §2.5
  VP-07: B4 §2.6, B4 §5
  VP-08: B4 §4
  VP-09: B4 §2.7, B4 §10
  VP-10: B4 §6
  VP-11: B4 §7, B4 §5
  VP-12: B4 §8
  VP-13: B4 §9
  VP-14: B4 §11, B4 §12
decision_crosscheck:
  sampled_blind_reapplication: DP-B4-1
  no_formal_dispute_state: DP-B4-2
  merge_only_spot_audit_in_v1: DP-B4-3
  conditional_referent_gap_rerun: DP-B4-4
  no_default_reruns: B4 §3, B4 §13
  bounce_cap_three: B4 §4, B4 §13
coverage:
  operational_v1: B4 §1–§9, B4 §11
  record_schema_sketch_only: B4 §10
  unresolved_not_filled: B4 §12
  rulings_checked: B4 §13
---

# Seam verifier procedure — v1

This is a standalone operational distillation of the verifier protocol. The step IDs
map to their source sections in the frontmatter; they are provenance, not a second set
of obligations. If this procedure and its source disagree, the source wins.

**B4-adjacent ruling changes trigger re-derivation of this doc.**

## VP-01 — Take the adjudication fresh

Begin each adjudication as a fresh verifier. Read the frozen report, ex-ante plan,
dispatch record including steers and interrupts, archives, node lineage, and current
standing. State lives in those records, not in you.

Adjudicate; never redirect. Name a defect and the evidence that would suffice to cure
it, but do not prescribe what the unit should research. Prefer demotion when the
evidence supports a weaker claim. Treat a negative result as successful research, not
as a defect. You may fan out independent help; sampled-blind tier-2 re-application is
the principal use.

## VP-02 — Confirm the mechanical submission gate

Confirm that `ht report submit` accepted and froze the report before substantive
adjudication. The mechanical gate parses required structure, resolves anchors, and
records the freeze hash. Do not silently compensate for a failed submission gate or
manually waive its invariants.

## VP-03 — Test plan conformance

Compare the report and archive with the ex-ante plan. An undeclared deviation found in
the archive is an adjudication failure. Account for logged steers and interrupts when
judging conformance: a steered dispatch is **truncated-by-direction**, never exhausted.

## VP-04 — Test coverage

Assess the done-definition against the plan's declared exhaustion criteria and the
work actually recorded. If the report claims broader coverage than the archive earns,
demote that coverage claim. A completable gap may become a report-level defect; an
honestly bounded result does not.

## VP-05 — Adjudicate every claim at its tier

For each proposed claim, grant it at the proposed tier, demote it with a reason, or
reject it with a reason. Never grant above the proposed tier.

For a **tier-1 point fact**:

- resolve its anchors to instrument output;
- confirm that the instrument version was legal for the evidence epoch;
- confirm that the required noise floor was applied; and
- perform a brief diff-and-reasoning review: could the recorded change plausibly
  produce the number, and does the number bear on the words of the claim?

Do not re-run by default. A re-run can reproduce a misleading number without testing
its meaning. Re-run only on concrete suspicion, or at the merge station when a
referent gap exists because supporting evidence predates later branch-tip changes.

For a **tier-2 episode claim**:

- confirm that its judgment rubric was timestamped in the ex-ante plan;
- have the anchored episodes sampled and the rubric re-applied blind to the senior's
  judgments;
- escalate to full re-application when the sampled judgments disagree; and
- check that the claim text does not outrun the sampled evidence.

The sampling parameters are deliberately unresolved. Do not invent a default count.

For a **tier-3 pattern claim**:

- derive its support count from traceable echoes;
- require genuine variety in the contexts that support it;
- actively search the tree for contradictions; and
- after those checks, require director approval and user ratification.

## VP-06 — Route residue without redirecting the review

Route demotion spin-offs and proposed hypotheses through ledger intake with its normal
deduplication. Route off-frame observations to the ledger when they are
hypothesis-shaped; otherwise retain them with the node record. Route operational notes
to the methodology lane. Routing preserves useful residue but does not change the
question being adjudicated.

## VP-07 — Set standing with a reason

Update node standing and write a mandatory rationale citing the claims or children
that drive it. Standing is judgment, never arithmetic:

- **supported** — a preponderance of granted evidence favors the premise and no
  contradiction remains unresolved;
- **weakened** — counterevidence is accumulating but is not decisive;
- **refuted** — a tier-1 refutation or repeated independent tier-2 refutations are
  decisive;
- **contested** — claims or children genuinely conflict and the rationale cannot
  resolve them; this flags director attention and is not a resting state; or
- **untested** — no adjudicated evidence bears on the premise.

Propagate a standing change up the lineage by re-evaluating each parent it affects.
At merge candidacy, one fresh verifier re-derives the merging branch's standing chain;
that is v1's lying-map guard.

## VP-08 — Choose the whole-report verdict and run the bounded repair loop

Use one of two whole-report verdicts:

- **accepted with adjudications** is the default and may include individual claim
  demotions or rejections; or
- **bounced with a defect list** is reserved for fixable evidentiary defects:
  unresolvable anchors, undeclared deviations, incoherent measurement-to-claim links,
  or completable coverage gaps.

Demotion is not a bounce. Rejection of an individual claim is not automatically a
bounce. A negative result is never a bounce merely because of its direction.

On a bounce, send the adjudication record directly to the unit, bypassing the director.
For every defect, state what is wrong and what evidence would suffice; do not prescribe
an experiment. The unit keeps context, repairs, and resubmits. Attempts are recorded on
the dispatch. At the cap of three attempts, escalate to the director with the complete
adjudication history; the director chooses whether to recall, re-pose, replace the unit,
or accept the residue.

## VP-09 — Leave the adjudication record

Write the adjudication record in the node's adjudications area. It carries the
dispatch, attempt, verifier instance, whole-report verdict, plan-conformance result,
coverage result, per-claim verdicts and reasons, bounce defects when present, routing
performed, and the standing change with rationale. On a bounce, this record is the
unit's feedback.

That field list is a schema sketch, not permission to invent the still-unresolved
command surface or final serialization details.

## VP-10 — Map watch outcomes

For **regressed**, mark the prediction claim contradicted, reassess standing toward
weakened or refuted as the evidence warrants, annotate the node, and surface it at the
director's next review.

For **under-delivered**, keep a claim that was true at its own epoch and scope, and
record the production gap. If the original claim was production-scoped, demote it
retroactively.

For **confirmed**, annotate the node and add an echo for any ledger entry the
confirmation validates.

## VP-11 — Staff the merge station

At merge candidacy:

1. confirm that every claim backing the candidate is granted and unexpired;
2. when later commits exist after supporting tier-1 evidence, re-run that evidence
   against the branch tip so it refers to what will merge; skip this for a
   single-dispatch branch with no referent gap;
3. confirm mechanically that all sibling conflict settlements are complete;
4. have one fresh verifier re-derive the merging branch's standing chain; and
5. require user ratification when the merge promotes actor-visible guidance.

## VP-12 — Apply the verifier guards

Watch demotion rate in both directions: near-zero demotion over many adjudications can
be as suspicious as high demotion. Never review your own prior work; fresh instances
perform spot-audits and re-derivations. Never shape the unit's plan or dispatch. Treat
the checker's QA trail as process evidence, never as a binding verdict.

Do not create a formal dispute process around claim decisions. Bounce and resubmission
are the repair loop, verifier decisions are final at the claim seam, and the user
override is the ceiling.

## VP-13 — Rely on the tool-enforced floor

The following are machinery invariants, not discretionary verifier steps: schema
conformance on state writes; role-to-field authority; at least one resolvable anchor
per claim; granted tier no higher than proposed tier; real epochs and epoch-legal
instrument versions; legal status transitions; settlement-complete merges; frozen
submitted reports with recorded hashes; write-once archives; and ledger creation by
the authorized role under the trigger/write split.

When machinery rejects one of these, preserve the defect. Do not simulate a manual
override or treat the rejection as a research judgment.

## VP-14 — Keep v1 lean and leave open mechanics open

V1 includes the report pipeline, tier-scaled checks, bounded bounce loop, watch mapping,
standing rubric and propagation, merge station with one fresh re-derivation,
demotion-rate monitoring, and tool-enforced validation. It does not add periodic
standing spot-audits, formal claim-dispute states, full-blind tier-2 re-application as
the default, or verifier-side batching and queueing. Those remain growth paths until
observed need justifies them.

Tier-2 sampling parameters, the escalation-at-cap message format, and the verifier's
adjudication command surface remain open. This procedure does not fill those gaps.
