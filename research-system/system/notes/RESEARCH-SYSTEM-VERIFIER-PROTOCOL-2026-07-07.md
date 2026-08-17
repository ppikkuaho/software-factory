# Hypothesis Tree Research System — Verifier Protocol (Design Area B4)

> **Status:** Working note; design converged 2026-07-07.
> **Parents:** `RESEARCH-SYSTEM-CONCEPT-2026-07-07.md`, `RESEARCH-SYSTEM-TREE-SCHEMA-2026-07-07.md`
> (A1 v2), `RESEARCH-SYSTEM-PHYSICAL-LAYOUT-2026-07-07.md` (A2),
> `RESEARCH-SYSTEM-SEAM-FORMATS-2026-07-07.md` (A3). Same directory.
> **Scope:** item 3 of the concept §13 queue. Closes the parked deferrals: standing
> rubric (A1 §11.3), watch-verdict mapping (A1 §5), mechanical validation rules
> (A1 §11.2).
> **Shaping rule (user, 2026-07-07):** leanest shape that works; expand only on
> observed need. V1 vs. growth-path split is explicit throughout (§11).

Tags: ✅ committed · 🟡 committed concept, mechanics open · 🔲 open.

---

## 1. Posture ✅

- **Stateless per adjudication.** Each adjudication is a fresh verifier instance
  reading: report, ex-ante plan, dispatch record (incl. steers/interrupts),
  archives, node lineage + standing. State lives in the tree, never in the
  verifier.
- **Adjudicate, never redirect.** Feedback names defects and what evidence would
  suffice — never what to research ("run experiment X" is the director's space).
- **Demote, don't reject** where the evidence supports a weaker claim.
- **A negative result is a successful dispatch.** "Hypothesis refuted" grants
  claims and weakens the premise — it is never a defect. Bounces are about report
  discipline, never the finding's direction. (Guard against verifier-induced
  claim inflation: units must never learn to iterate until results look positive.)
- **May fan out subagents** (ruled earlier) — tier-2 re-application is the main
  use.

## 2. Adjudication pipeline (per submitted report) ✅

1. **Structural gate** (mechanical, pre-verifier — `ht report submit`): fenced
   blocks parse, anchors resolve, required sections present, freeze hash recorded.
2. **Plan conformance** — report vs. ex-ante plan. Undeclared deviations found in
   the archive = adjudication failure (A3 DP-A3-3). Steers/interrupts adjust
   expectations: a steered dispatch adjudicates as *truncated-by-direction*, never
   "exhausted."
3. **Coverage** — done-definition assessment vs. declared exhaustion criteria.
   Over-claimed coverage is demotable like any claim.
4. **Per-claim adjudication** — tier-scaled (§3): grant at proposed tier / demote
   with reason / reject with reason. Never grant above proposed tier.
5. **Routing** — demotion spin-offs + proposed hypotheses → ledger (D8 dedup at
   intake); off-frame observations → ledger if hypothesis-shaped, else node
   record; operational notes → methodology lane.
6. **Standing update** — node standing + mandatory rationale; propagate up
   lineage (§5).
7. **Adjudication record** → `nodes/<id>/adjudications/d-<id>-N-a<attempt>.md`
   (§10). On a bounce verdict, this record IS the feedback to the unit (§4).

## 3. Tier-scaled checks ✅

| Tier | Checks | Cost |
|---|---|---|
| 1 — point fact | Anchors resolve to instrument output · instrument version legal for epoch · noise floor applied · **brief diff + reasoning review**: does this change plausibly produce this number, and does the number bear on the claim text? | Cheap |
| 2 — episode claim | Rubric declared ex-ante (timestamped in plan) · **sampled-blind re-application** of the rubric to anchored episodes, blind to the senior's judgments; escalate to full re-application on disagreement (DP-B4-1) · scope check: text doesn't outrun evidence | The cost center; fan-out here |
| 3 — pattern claim | Support count from traceable echoes · context-variety check (same effect across genuinely different conditions) · active contradiction search across the tree · then director approval + user ratification | Rare, expensive by design |

**No default re-runs** (user ruling): re-running a measurement reproduces a
misleading number faithfully. The tier-1 risk is not "the number was flaky" but
"the number is real and doesn't mean what's claimed" — which the diff/reasoning
review targets. Re-run only on concrete suspicion, and at the merge gate's
referent-gap case (§7).

## 4. Bounce loop ✅ (user-ruled shape)

Two whole-report verdicts:

- **accepted (with adjudications)** — the default; includes any demotions and
  rejections of individual claims. Most reports land here in one pass.
- **bounced (defect list)** — reserved for **fixable evidentiary defects**:
  unresolvable anchors, undeclared deviations, incoherent measurement→claim links,
  completable coverage gaps.

Loop mechanics:

- The adjudication record (defect list + what evidence would suffice) goes to the
  unit directly — a legal verifier↔unit seam edge, written and logged, **bypassing
  the director** until the cap.
- The unit keeps its context, fixes, resubmits. Attempts tracked on the dispatch
  record.
- **Cap: 3 attempts.** At the cap the verifier **escalates to the director with
  the full adjudication history**; the director decides — recall, re-pose, replace
  the unit, or accept the residue.
- Guards restated: demotion ≠ bounce; negative result ≠ bounce; feedback
  adjudicative, never directive.

(Same shape as the L1-L5 L5/L5+ bounded bounce; convergence noted, machinery not
imported — A3 §7 I-7 stands.)

## 5. Standing rubric ✅ (closes A1 §11.3)

Standing = verifier judgment with **mandatory rationale citing the claims/children
that drive it** — never arithmetic.

- **supported** — preponderance of granted evidence favors the premise; no
  unresolved contradiction.
- **weakened** — countervailing evidence accumulating, not decisive.
- **refuted** — decisive: a tier-1 refutation, or repeated independent tier-2
  refutations.
- **contested** — children/claims genuinely conflict and rationale cannot resolve
  them. A flag for the director, not a resting state.
- **untested** — no adjudicated evidence bears on the premise.

Propagation: a standing change re-evaluates the parent, recursively (damps within
a level or two in practice). **Lying-map guard:** v1 = one fresh re-derivation of
the merging branch's standing chain at merge candidacy (§7); per-N sampled
spot-audits are growth-path (§11).

## 6. Watch-verdict mapping ✅ (closes A1 §5 guidance)

- **regressed** → prediction claim `contradicted`; standing typically →
  weakened/refuted (verifier judgment, rationale required); node annotated;
  surfaces at the director's next review.
- **under-delivered** → the original claim stands (true at its epoch and scope);
  the annotation records the production gap; if the claim text was
  production-scoped, that is a retroactive demotion.
- **confirmed** → annotation; echo for any ledger entry it validates.

## 7. Merge gate ✅ (the verifier's second station)

1. All claims backing the candidate are granted and unexpired.
2. **Referent-gap re-verify** (conditional): where supporting tier-1 evidence
   predates the branch tip (later commits exist), re-run that evidence against the
   tip — the claim must describe the thing actually merging. Single-dispatch
   branches skip this entirely (re-run adds nothing there; user ruling).
3. Settlement completeness — mechanical (all sibling conflict entries resolved).
4. One **fresh standing re-derivation** of the merging branch's chain (v1's only
   mandatory spot-audit).
5. User ratification where the merge promotes actor-visible guidance.

## 8. Guards on the verifier ✅

- **Demotion-rate watched in both directions** (readout, free — fields exist):
  near-zero demotion across many adjudications is as suspicious as high demotion.
- **No self-review:** spot-audits/re-derivations use fresh instances; the verifier
  never shapes plans or dispatches.
- **Over-severity path:** no formal dispute machinery — bounce/resubmit is the
  loop, the user override is the ceiling. Verifier decisions are final at claim
  level so the seam stays cheap. (DP-B4-2 dissolved into the bounce loop.)
- Checker QA trail = evidence of process, never binding.

## 9. Mechanical validation rules ✅ (closes A1 §11.2 — what `ht` rejects outright)

- Schema conformance on every state write (`system/schemas/`).
- Role × field authority per A1 §10 (write-domain map; D10).
- Claims must cite ≥1 resolvable anchor; anchors must resolve at submit time.
- `granted_tier ≤ proposed_tier`.
- Epoch stamps must reference real epochs; instrument versions must be legal for
  the stamped epoch.
- Status-transition legality per the A1 §2.1 graph (e.g. no `unexplored → merged`;
  `parked` exits only via settlement).
- Merge blocked while any sibling conflict entry is `pending`.
- Report freeze: post-submit edits impossible; submission hash recorded.
- Archive write-once (tool-enforced; archives live outside git — A2).
- Ledger entry creation only by its authorized author incl. trigger/write split
  (A1 §7).

## 10. Adjudication record 🟡 (schema sketch; format finalized with `ht` build)

```
adjudication
├─ dispatch, attempt, verifier instance id
├─ verdict: accepted | bounced
├─ plan_conformance: ok | defects[...]
├─ coverage: met | over-claimed(demoted) | truncated-by-direction
├─ per-claim: [{claim, verdict: granted|demoted(tier, reason)|rejected(reason)}]
├─ defects (bounce only): [{what, what-evidence-would-suffice}]
├─ routing: [ledger entries created, methodology notes filed]
└─ standing_delta: {node, from, to, rationale}
```

Home: `nodes/<id>/adjudications/` — epistemic domain, verifier-authored. Dispatch
record gains `adjudications: [refs]` (attempt count derivable).

## 11. V1 vs growth-path ✅ (lean-first)

| V1 | Growth-path (on observed need) |
|---|---|
| Pipeline §2; tier checks §3; bounce loop §4 | Per-N sampled standing spot-audits |
| Merge gate §7 incl. one fresh re-derivation | Formal dispute states on claims |
| Demotion-rate monitoring via readout | Heavier tier-2 defaults (full-blind re-application as default) |
| Validation rules §9 in the `ht` tool | Verifier-side batching/queueing machinery |

## 12. Open within this area 🔲

1. Verifier role packet + handbook section (with A3 §9.1–9.2).
2. Tier-2 sampling parameters (how many episodes; blind protocol details) — tune
   from the first real adjudications.
3. Escalation-at-cap message format (small; with `ht` build).
4. `ht` command surface for adjudication (grant/demote/bounce/standing) — build
   work, Phase B remainder.

## 13. Decision log

| Ruling | Decision | Date |
|---|---|---|
| DP-B4-1 | Tier-2 = sampled-blind re-application; full on disagreement | 2026-07-07 |
| DP-B4-2 | Dissolved into the bounce loop (user shape): feedback-with-reasoning → fix → resubmit; no formal dispute state | 2026-07-07 |
| DP-B4-3 | Spot-audits: v1 at merge candidacy only; per-N sampling growth-path | 2026-07-07 |
| DP-B4-4 | Merge re-verify conditional on referent gap (claims predate branch tip); skipped for single-dispatch branches | 2026-07-07 |
| Tier-1 checks | No default re-runs; brief diff + reasoning review instead (user ruling: re-runs near-useless; risk is meaning, not flakiness) | 2026-07-07 |
| Bounce cap | 3 attempts, then verifier escalates to director with full history | 2026-07-07 |
| Guards | Negative-result-≠-bounce; demotion-≠-bounce; adjudicative-not-directive feedback | 2026-07-07 |
| Shaping | Lean-first: v1/growth split per §11 | 2026-07-07 |
