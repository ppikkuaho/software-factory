# Coherence Amendments — 2026-07-12 (post-review)

> **Status:** adopted 2026-07-12 from the three-review pre-dispatch pass
> (`REVIEW-SCENARIO-TRACE-`, `REVIEW-AUTHORITY-AUDIT-`,
> `REVIEW-PACKET-STYLE-2026-07-12.md`). §1 (B1) and §2 (F7) are
> **user-ruled**; the remainder drafter-adopted under the reviews'
> resolutions with the user informed. This note **amends the named sections
> of the 2026-07-07 and 2026-07-12 notes by reference** (dated notes are
> never rewritten). **Binding on the overnight foundations build** (run
> brief work item 0). Together with the three reviews, this note
> **discharges the macro note's §9.8 second-pass coherence check.**

## 1. B1 — Cascades: the issue is the spine (USER-RULED, with playbook)

Option (a): no cross-lane tree nodes. A cascade is a **PC-owned issue**
fanning to **linked per-lane hypotheses** (`issue_ref` both directions, §4);
evidence accumulates in lane trees under normal machinery; the cross-level
synthesis lives in the issue's closure record (PC-authored, navigation
domain); the composed-tree projection displays the join. The LCA rule
applies to issue **scope**. First-class upper tree remains the named growth
path — trigger: recurring cascade issues, or closure records visibly
straining to carry cross-level synthesis.

**The cascade playbook (user-designed, the default protocol for cascade
issues):**

1. **Frame rival attributions.** The PC frames the issue as rival
   attribution hypotheses — e.g. H-A: cause on the producing side (the
   packages mislead); H-B: cause on the consuming side (misreads despite
   sound packages — missing context etc.). Framing is navigation (the
   issue's question), not epistemic assertion; lanes mint and test the
   actual hypothesis objects.
2. **Optional stage 0 — cheap attribution characterization:** a shallow
   dispatch over existing behavioral records (deliverable: structure /
   attribution evidence, not a verdict) before any intervention testing.
3. **Sequential chase.** Sub-goal to ONE lane first. Ordering is the PC's
   judgment — cheapest-to-test, evidence already in hand, smallest blast
   surface — logged in the decision log (contradictable later).
4. **Conditional advance.** Supported → proceed to intervention on that
   side; issue heads to closure. Refuted/weakened → stage 2 sub-goal to the
   other lane, whose dispatch **premise chain carries the stage-1 verified
   findings** — cross-lane evidence transfer rides the existing
   premise-chain mechanism (concept §8.2); each hop's premises are
   adjudicated claims, never narrative.
5. **The third outcome — interface attribution.** Both sides refuted →
   the cause is the seam itself (the contract between the seats
   under-specifies; neither seat is "wrong"). A contract-surface hypothesis
   is minted in the lane owning that surface; PC logs the call. If that too
   exhausts → escalate to the user with the full trail.
6. **Done-definition template for cascade issues:** "closed when the
   cascade has an attributed cause with a supported lane hypothesis and a
   merged-or-demoted intervention — or all attributions refuted and
   escalated."

## 2. F7 — Improvement-note gate (USER-CONFIRMED)

The user gate is **triage, not authorship**: the graded-check producer
appends to the queue; the user disposes; on admit the note enters the
ledger's **observatory section via the verifier** (provenance preserved).
Queued notes are uncitable until adjudicated. (Amends the RQ-1 third-round
mechanism's landing path; observatory §1's "nothing enters except through
the verifier" is thereby preserved, not amended.)

## 3. Merge mechanism re-homed (S1/F8)

New object: **`merge_record`** per merge — candidate ref, lane verdict,
screen results, gate verdict, watch link. Screen fields harness-authored;
verdict fields `cgate`-authored. **`ht` holds the global merge mutex and is
the sole epoch writer**, invoked only on a composition-gate `land` verdict
(the epoch write gains that precondition). *Amends A1 §5/§10 merge-gate
row.*

## 4. Issue↔node join (S2)

`minted_from` gains `issue#…`; the dispatch record gains `issue_ref`.
Bidirectional provenance for the composed tree and closure records.
*Amends A1 §2/§3.*

## 5. Ratification queue as a first-class object (S3)

`ht`-written, append-only. Authorized appenders: `cgate` (escalations),
verifiers (tier-3 ratifications), the graded-check producer (improvement
notes), PC (activation requests). The user disposes. The PC reads,
annotates, and notifies — never disposes. *PC note §7 "nothing else"
clarified accordingly.*

## 6. Issue closure protocol (S4)

Closure enumerates in-flight sub-dispatches and blocks until each is
terminal: completed, recalled (**by its own lane director, at PC request**),
or demoted. Issue-scope settle = per-sub-goal dispositions recorded on the
closure record.

## 7. Whole-run observatory ownership (S5)

**One shared whole-run observatory pass** (system-wide in operation),
routing per-level findings into per-level books. Per-lane observatories own
slice/synthetic runs only. *Amends the macro §4 observatory bullet's
emphasis.*

## 8. Authority extension (F1/F2/F3/F9/F12 — the load-bearing build item)

- Tier-1 authority block extending **A1 §10**; `HT_ROLE` gains `pc` and
  `cgate`; the pre-commit hook path map gains the **`tier1/`** store
  (issue queue, decision log, closures, ratification queue, merge records).
- **Verifier is a CLASS credential.** Cross-level (top-book) ledger entries
  are authored by the **LCA-docked lane's verifier**; cross-book
  echo/support appends are **atomic with their dedup** in `ht` (A1 §7 gains
  a book dimension).
- Issue activation transition is **phase-flagged**: sign-off mode → user;
  autonomy mode → PC.

## 9. Settlement trigger/write split (F4)

The global merge **queues** staleness assessments; the **owning lane's
director executes** the writes into its own nodes; the PC owns only a
distinct cross-lane arbitration record — never a direct write into a lane
tree.

## 10. Evidence classing (F5/F6)

`standing_class: trunk | sandbox | slice` on measurements and claims. The
merge gate **mechanically rejects** candidates whose backing claims carry
`standing_class ≠ trunk` without a re-validation record (mechanized; not a
D7 exception).

## 11. Generated-state discipline (F10)

Composed tree = **deterministic union**, no standing re-derivation (else the
PC becomes a second epistemic author). Loop-effectiveness readout =
harness-computed, uncitable, interpretation rules co-located.

## 12. Director→PC blocking interrupt (F11)

One legal upward interrupt: "this sub-goal cannot be completed meaningfully
as posed" — mirroring the unit→director interrupt (concept §8.4); logged in
the PC decision log.

## 13. Minors (M1–M3)

Expectation staleness propagates to queued improvement notes
(flag/re-validate/withdraw at re-derivation). Intra-lane sub-goal fan-out
serializes on that lane's capacity-1 cursor — issue-level concurrency is
across lanes. Issues reference the ledger entries they were drafted from
(dedup lives at the ledger; report-card provenance goes through it).

## 14. Amendment-table extension (F13)

Three rows added to macro §11's table by this note: **A1 §7** (ledger
authority partition — amended per §8 above); **A1 §10** (authority table —
extended per §8); **concept §1/§4.2** (sequential-execution framing —
amended to issue-level capacity-1 with cross-lane concurrency; the
concept's own pre-authorized scheduler change, now named).

## 15. §9.8 discharge

The three reviews plus this note constitute the second-pass coherence
check. **Discharged 2026-07-12.**
