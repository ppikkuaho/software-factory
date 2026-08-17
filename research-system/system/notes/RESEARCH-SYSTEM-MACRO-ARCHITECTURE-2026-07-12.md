# Hypothesis Tree Research System — Macro Architecture (Whole-System Tier)

> **Status:** working note; concept-level design, converged 2026-07-12 (design
> session, user + Fable seat). **Confirmed by user read-back 2026-07-12**
> (walkthrough incl. the §10 drafter calls and §11 amendment table; drafter
> calls stand unless later struck). Second-pass coherence check still owed
> (§9.8).
> **Extends:** `RESEARCH-SYSTEM-CONCEPT-2026-07-07.md` — gives concrete shape to
> its §3.4 (one tree per component; recursion upward, previously 🟡
> mechanics-open) and **amends** the singleton-machinery model (see §11 for the
> explicit amendment list — nothing here silently overwrites ratified state).
> **Source:** user sketch from the private development workspace (not included) +
> the 2026-07-12 dialogue.
> **Purpose:** freeze the converged macro shape so design can continue without
> re-derivation. NOT a buildable spec; §9 is the open-question queue.

---

## 0. How to read

Same convention as the concept note: ✅ committed/ruled · 🟡 committed concept,
mechanics open · 🔲 open, do not assume. Decision log at §10 separates user
rulings from drafter judgment calls.

---

## 1. The gap this layer fills ✅ (user-stated, 2026-07-12)

The 2026-07-06/07 design produced the **unit-level engine** — tree, director,
unit, seam verifier, observatory — and it is good at what it aims at: micro-level
research on one unit of the subject system (execution-L3's behavior, L4's
behavior, …). What it lacks is the **structure / separation of concerns /
bigger-picture view**. Three reasons (user):

1. **Cascades** — issues often span multiple levels and need to be looked at
   comprehensively, not per-component.
2. **Coordination** — without a central repository, coordinating many
   improvement lanes is difficult.
3. **Holism** — without a standing whole-system-view function, the harness can
   structurally only ever produce component-level improvements.

The macro layer is a **coordination and orchestration substrate**: it routes
work to the unit levels and offers more views and organizing axes. The actual
research/iteration still happens **mainly at the unit level**, on the engine
already designed. Blast-surface identification lives at the top but is
secondary in emphasis — it is the anti-myopia discipline (don't assume the fix
belongs at the level where the symptom appeared), not a targeting subsystem.

---

## 2. Notation ✅ (user ruling 2026-07-12)

- **Ln** — execution seat at level n (e.g. L4).
- **Ln+** — review/verification seat at level n (e.g. L5+, L3+).
- **Ln&** — **planning seat** at level n (new notation; e.g. L2& = L2's
  planning function). Planning seats exist at L1–L3.

The improvement harness's unit taxonomy mirrors the main system's actual seat
taxonomy (planning / execution / review), not just its level numbers.
Notation propagation: **DONE 2026-07-12** in both corpora (keys in the concept
§15 glossary and in the L1-5 corpus per its `BRIDGE-2026-07-12b.md`; that
bridge parks **8 ambiguous-seat questions awaiting the user**).

---

## 3. The three tiers ✅ concept / mechanics 🔲

The macro architecture deliberately parallels the L1-L5 system itself:
direction flows down, results flow up, and tiers separate by **kind of
thinking** — the top tier is not a bigger unit-system, it is a different
cognitive job. (Precedent: the main ARCHITECTURE.md already anticipated an
"Improvement Workspace L1" whose projects are the system itself.)

### Tier 1 — whole-system view ✅

The **primary coordination / identification surface. This is what drives the
system.** Its subject is the entire main system across all three seat
families: **L1&–L3&** (planning), **L1–L5** (execution), **L2+–L5+** (review).
Owns: the holistic view, cross-level cascade subjects, the union ledger view
and its convergence diagnostics (§5), merge scheduling and settlement
arbitration (§6), the merge composition gate (§7), and lane-director health
judgment over the rolled-up monitor analytics (§4).

### Tier 2 — decomposition layer 🔲

Whether execution and planning need their **own intermediate decomposition
layer**, or whether Tier 1 decomposes directly into level components, is open.
Either way, **planning and execution+verification stay separated**.

### Tier 3 — unit-level improvement systems ✅

**Boxes within boxes:** first the level box (e.g. the L2 level = L2 and L2+
together — the *pair* is a real subject: bounce dynamics, review independence,
rubber-stamping are pair phenomena neither seat owns alone), then L2 and L2+
as separate subjects within it. **Each unit gets its own improvement system** —
this is where the previously-designed engine slots in: each unit is a subject
lane (component tree) with live machinery. This is the bolt-on point; the
unit-level engine is not redesigned by this note.

Tier 1 and Tier 2 structures **may need their own design matched to their
tasks** (user) — their "tree" may not be a hypothesis tree at all 🔲.

---

## 4. Fractal replication: what replicates, what stays shared ✅

### Replicates per unit/box (user ruling 2026-07-12)

- **Director** — each level/lane gets its own. This exercises the concept's
  pre-authorized relaxation ("cursor = attention allocation with capacity 1
  … later parallelism is a scheduler change, not a schema change"): attention
  becomes something **Tier 1 allocates downward**. Amends concept §3.4's
  one-cursor statement — see §11.
- **Observatory** — per-lane stance. A lane may run **its own slice** of the
  subject system (e.g. just the L3 part) on replay or synthetic data and
  observe that slice directly. Discipline: slice/synthetic runs carry their
  run-kind and exposure labels; slice evidence licenses slice-scoped claims
  only — promotion to live-behavior claims requires live evidence.
- **Monitor** — per-lane, as instruments (v1 instrument-only ruling
  preserved). Analytics roll up to Tier 1, which can watch lower levels as
  needed. Rails: a monitor never gains authority over its own level's
  director; alerts route **up**, never sideways into enforcement; judgment
  sits one level above the thing judged; the user closes the recursion at the
  top via frontier reviews.
- **Seam verifier** — per lane (ruled 2026-07-12): per-dispatch machinery
  scales with lanes. The mechanical validation rules and the `ht` write gate
  remain **one shared implementation** (methodology lane); independence rules
  carry over unchanged. The composition gate (§7) is *not* a super-verifier —
  lane verifiers own claim adjudication; the gate owns composition only.
- **Trees** — already per-lane (unchanged).

### Stays shared / singular

- **Evidence substrate + instruments** — one trace extraction per run, one
  trace reader, one methodology lane. Replicate the *stance*, share the
  *instrument and the raw record* (otherwise N observatories re-extract the
  same runs and drift at the evidence layer). Shared instruments, private
  samples: lanes point the shared trace reader at their own runs.
- **Ledger** — one *logical* ledger; federated storage (§5).
- **Trunk, epoch counter, merge serialization, settlement** — reaffirmed
  global and singular (§6).

Summary: **fractal seats, shared spine.**

---

## 5. The ledger: federated books, one namespace ✅ (user amendment 2026-07-12)

Supersedes the single-file reading of "one shared ledger" (A2 §5.4) while
preserving its rationale.

- **Per-level books** — each level keeps its own ledger; L2's book stays
  L2-focused for L2's director (user).
- **Top-level union view** — Tier 1 reads a shared aggregated view (symlinks
  into a top folder, a generated index — implementation detail).
- **Cross-level subjects** are homed in the top book.
- **One logical ledger, enforced by three requirements** (drafter call —
  these are what keep A2 §5.4's convergence rationale intact):
  1. global entry IDs across all books;
  2. proposal-time dedup / convergence checking (D8) runs across the **union**
     of books, never locally only;
  3. Tier 1's diagnostics are projections over the union.
- **Anti-silo rule:** the failure mode this guards is local-only echo
  detection — the L3 book and L5 book each holding half of the same
  system-wide disease ("agents don't trust their orientation material")
  forever, invisibly.

---

## 6. Trunk and merge economics ✅ (reaffirmed + two additions)

Reaffirmed unchanged (user, 2026-07-12): **one trunk; one global epoch
counter; merges serialize; settlement per A2 §5.3.** Freezing never blocks
work — every lane works on branches (a "wider change" is a stacked branch);
work and measurement are fully parallel; only merges serialize, and a merge
does not wait for other lanes — it lands and triggers settlement. The
settlement mechanism (cheap per-branch staleness assessment; re-measure /
accept loss / demote to ledger; each "no effect" recorded as decoupling
evidence) was confirmed by the user as answering the parallelism/wall-clock
concern.

Two additions:

- **Merge scheduling is a Tier 1 job** — batch merges into windows; hold a
  merge briefly while a lane's expensive measurement campaign completes.
  Coordination-plane work; no epistemic authority involved.
- **Sandbox trunks 🟡 (drafter call, direction accepted in discussion):** for
  long-horizon divergent exploration a lane may fork a private trunk, with
  evidence standing **explicitly demoted** — results are directional only
  (measurements of a different system) and must re-validate against the real
  trunk before merge. Never silently equivalent to trunk evidence. Default
  remains branches-off-the-one-trunk.

---

## 7. The merge composition gate ✅ concept / 🔲 packet + triggers (user proposal 2026-07-12)

An "**L+ equivalent for research**" — in the notation, **Tier 1's + seat**.

- The lane-level merge gate (verifier's second station, B4 §7) stays local:
  "is this merge sound on its own terms."
- The composition gate reviews **what only the elevated altitude can see**:
  1. interaction effects between merges (individually-clean changes that
     collide semantically);
  2. cumulative surface budget — directive accretion across merges on
     actor-visible surfaces (the effective-surface audit, inherited from the
     2026-06-21 corpus, becomes this seat's standing axis);
  3. sequencing/consolidation of pending merges (pairs with Tier 1's merge
     scheduling);
  4. cross-lane settlement adequacy and global staleness posture;
  5. portfolio honesty over time — merge rate vs. what post-merge watches
     actually confirm in production.
- **Rails** (drafter calls, both inherited from existing rulings):
  - **altitude discipline** — it never re-adjudicates lane-level claims (the
    "upper gate redoing lower work" pathology); its verdicts are
    compositional: land / sequence-after / consolidate-first / bounce for
    surface-budget rework;
  - **gates, doesn't steer** — it can hold or bounce a merge, never redirect
    research; otherwise it becomes a second director.
- **Lean-first:** v1 default is a cheap mechanical screen (scope-axis overlap,
  effective-surface diff, pending-merge adjacency), escalating to full
  elevated review on flags. Trigger table + review packet: 🔲 (packet is
  definable almost entirely from existing artifacts: effective surfaces,
  merge history, settlement records, post-merge watch results).
- **Full authority from day 1 (ruled 2026-07-12):** no shadow period — the
  gate screens and can hold from the first merge. Ruled explicitly to
  minimize reliance on user availability. Design consequence: the gate must
  be self-sufficient — pre-set decision rules where possible (the proven seam
  practice), and `escalate-to-user` verdicts **queue asynchronously**
  (ratification-queue pattern) holding only the affected merge, never
  blocking unrelated ones.

---

## 8. Governance continuity ✅

- **The write-domain split extends upward unchanged.** Tier 1 holds
  navigation/coordination authority — routing, minting, attention allocation,
  merge scheduling — and **never authors epistemic content**; its views are
  generated and uncitable, same standing as always. It must not become a
  second epistemic author or a shadow lab, or the self-confirmation problem
  is recreated one level up.
- **Who-watches-whom moves up one level per tier**, mirroring the main
  system: lane monitors → Tier 1 judgment; Tier 1's own policy health → the
  user at frontier reviews. Human closes the loop at the top.

---

## 9. Open questions 🔲 (the work queue for the next pass)

1. ~~Realization of Tier 1~~ — **RESOLVED 2026-07-12 (user ruling): standing
   Tier-1 agent from day 1, user-handheld** ("if we don't, that's going to add
   unnecessary pain and clog things up"). Its internal design remains open
   under item 4.
2. ~~Whether Tier 2 earns its keep~~ — **RESOLVED 2026-07-12: axis, not
   organ.** Plane separation lives in views/topology; plane-level seats or
   nodes are minted only on observed clustering pressure.
3. ~~Attention-allocation mechanics~~ — **RESOLVED IN SHAPE 2026-07-12 (user
   ruling): capacity-1 at the *issue* level.** Tier 1 dispatches **one
   top-level issue at a time**; an issue may decompose into multiple
   concurrent unit dispatches beneath it. V1 enforcement mostly via prompts —
   an explicit, ruled D7 feasibility-caveat exception; a cheap `ht`
   issue-object guard is the standing mechanization candidate. Scheduler
   formalization deferred to observed contention.
4. **Tier 1 / Tier 2 internal structure** — Tier 1 **RESOLVED at concept
   level 2026-07-12**: the seat is the **principal coordinator**; full design
   in `RESEARCH-SYSTEM-PRINCIPAL-COORDINATOR-2026-07-12.md` (thin-router
   posture, issue object, inbox-hook routing, sign-off→autonomy, composed
   tree + loop-effectiveness readout, commissioning first run). Tier 2 has no
   organ (item 2), so nothing further owed there.
5. **Composition gate** review packet + trigger table.
6. ~~Docking of cross-level cascade subjects~~ — **RESOLVED 2026-07-12:
   lowest-common-ancestor rule.** A cascade hypothesis docks at the lowest
   tree node whose subtree contains every implicated seat; its dispatches may
   descend into lane slices for evidence.
7. ~~Notation propagation~~ — **RESOLVED 2026-07-12: cascade corpus-wide**
   (research-system corpus AND the L1-L5 corpus, each under its own write
   protocol), executed as a separate-session task; brief issued 2026-07-12.
8. ~~Second-pass coherence check~~ — **DISCHARGED 2026-07-12**: three
   independent review agents (scenario field-trace, write-authority audit,
   packet style — `REVIEW-*-2026-07-12.md`) + the consolidated
   `RESEARCH-SYSTEM-COHERENCE-AMENDMENTS-2026-07-12.md` (B1 cascade ruling +
   playbook, F7 gate path, mechanism re-homing, authority extension). The
   amendments note also extends this note's §11 table (its §14).
9. ~~Build sequencing~~ — **RESOLVED 2026-07-12 (user ruling):
   foundations-first.** Foundations land before the first research dispatch;
   growth-path items stay trigger-gated. **Scope RATIFIED 2026-07-12 (fourth
   round), as amended by the RQ pass** (RQ-RULINGS note): (1) digest v2
   template + regeneration [RQ-2]; (2) spine v1 — derivation mechanism + L4
   active set + improvement-notes user gate w/ daily notification [RQ-1];
   (3) role packets v1 — role-purpose framing on the shared-blocks substrate
   [RQ-4]; (4) ledger federation — global IDs, per-level books + union view,
   union-wide D8; (5) the issue object — schema + minting path, capacity-1
   per §9.3; (6) the Tier-1 agent — role packet, surfaces, decision-log
   discipline, bus seat (design session first); (7) composition gate —
   screen + trigger table + pre-set rules + async escalation queue, full
   authority (design session first); (8) **Codex-format trace adapter**
   (C6 §9.4 — user-dispatched from a private development brief, not included); (9)
   audit-log wiring (defect-flow unlock, from the Jul-7 handoff). Per-lane
   namespace scaffolding folded into (4)/(5). Explicitly NOT foundations
   (trigger-gated): per-lane director/observatory instances beyond the first
   lane (trigger: second tree), plane nodes, ledger-keeper, autonomous
   monitor, the run-completion hook, multi-cursor scheduler.

---

## 10. Decision log

### User rulings (2026-07-12 session)

- The gap: unit engine is micro-level; macro structure / separation of
  concerns / whole-system view is missing. Three motivations (§1).
- Three-tier shape; Tier 1 is the **primary coordination/identification
  surface that drives the system**; architecture parallels the L1-L5 system.
- Blast-surface identification at the top, de-emphasized (anti-myopia, not
  targeting).
- Boxes-within-boxes unit granularity: level box (e.g. L2 ∪ L2+) first, then
  per-seat subjects. Bolt onto the existing unit engine; work happens mainly
  at unit level.
- **& notation** for planning seats (§2).
- **Each level gets its own director and its own observatory**; Tier 1/2 may
  need their own designs matched to their tasks.
- **Monitors replicate too**, as analytics instruments rolling up to Tier 1,
  which can watch lower levels when needed.
- Instruments/evidence substrate designed for **sharing**; lanes may run and
  directly observe their own slice (replay/synthetic).
- **Ledger federated**: per-level books + shared top-level view (possibly
  symlinked); L2 book stays L2-focused; the top level handles wider views and
  diagnostics.
- **Global epoch counter** confirmed.
- Settlement mechanism confirmed as answering the parallel-work concern.
- **Top-tier merge review seat** ("L+ equivalent for research"): local merge
  confirmation is insufficient when multiple merges interact; elevated,
  holistic-axis review needed.
- **Second round (2026-07-12, post-read-back confirmation):** Tier 1 is a
  **standing agent from day 1, user-handheld**. Tier-2-as-axis accepted.
  Per-lane verifier replication accepted. Concurrency sized around **issues**:
  one top-level issue dispatched at a time, decomposing into multiple
  concurrent dispatches; v1 enforcement mostly via prompts (explicit D7
  feasibility-caveat exception). Notation cascade ruled corpus-wide (both
  corpora), run as a separate session.
- **Third round (2026-07-12):** LCA docking rule confirmed. Composition gate
  runs with **full authority from the first merge** — no shadow period
  ("don't rely on me too much"). **Foundations-first build sequencing** (§9.9;
  scope list is drafter-elaborated, pending trim/confirm). **First tree = L4
  execution seat** confirmed, with two riders: the Codex trace adapter is to
  be fixed regardless ("before moving forwards" — promoted onto the
  foundations path), and L3's known issues are already being addressed in the
  main-system tuning work. **Run-completion hook stays on-demand for now**;
  hook install deferred to a later ruling. Drafter note on the on-demand
  consequence: coverage is selective, so recurrence/support statistics must
  carry their denominator ("N of M *observed* runs", observed-run set
  recorded), or base-rate claims will silently overstate.

### Drafter judgment calls (proposed in-session; confirm or strike)

- The three one-logical-ledger requirements (global IDs; union-wide D8;
  Tier 1 diagnostics as union projections) as the condition on federation
  (§5).
- Slice-run discipline: run-kind/exposure labels; slice evidence licenses
  slice-scoped claims only (§4).
- Sandbox-trunk rule: allowed for long-horizon exploration with explicitly
  demoted, re-validate-before-merge evidence standing (§6).
- Merge scheduling assigned to Tier 1 (§6).
- Composition-gate rails (altitude discipline; gates-don't-steer) and
  lean-first trigger posture (§7).
- Monitor rails: zero authority preserved; alerts route up, not sideways;
  judgment one level above (§4).
- Write-domain split extended verbatim to Tier 1 (§8).
- "Tiers differ by kind of thinking" as the design principle for Tier 1/2
  structure (§3).

---

## 11. Amendments to prior ratified state (explicit; nothing silent)

| Prior state | Disposition |
|---|---|
| Concept §3.4 "only one cursor in the whole system" | **Amended**: per-lane directors; attention allocated downward by Tier 1. Uses the concept's own pre-authorized "scheduler change, not schema change" path. |
| Singleton director / observatory / monitor (concept §4.1, §7) | **Amended**: replicated per lane/box. Per-seat write-domain split unchanged. |
| A2 §5.4 "one shared ledger" (single store) | **Amended**: federated per-level books + top union view, under the §5 one-logical-ledger requirements. Rationale (cross-lane convergence signal) preserved. |
| Global trunk freeze; one epoch counter; merge serialization; settlement (A2 §5.1–5.3) | **Reaffirmed unchanged.** |
| Monitor v1 = instrument, zero authority | **Reaffirmed**, now replicated per lane. |
| Verifier merge gate (B4 §7) | **Reaffirmed** as the local station; composition gate added above it (§7). |

---

## 12. Glossary additions

- **Macro tier / Tier 1** — the whole-system improvement view: primary
  coordination and identification surface of the improvement harness.
- **Ln&** — planning seat at level n (notation ruled 2026-07-12).
- **Level box** — the pair-granularity subject (e.g. L2 ∪ L2+) containing
  per-seat subjects.
- **Lane book / union view** — a level's own ledger; Tier 1's aggregated
  projection over all books.
- **Composition gate** — Tier 1's + seat: elevated-altitude merge review
  across the holistic axis.
- **Sandbox trunk** — a lane's deliberate private trunk fork; evidence
  directional-only until re-validated against the real trunk.
- **Issue** — Tier 1's work atom (ruled 2026-07-12): one top-level concern
  dispatched at a time; decomposes into multiple concurrent unit dispatches
  beneath it. Schema and ownership design pending (with §9.4).

---

## 13. Addendum — second-round rulings + propagation record (2026-07-12, evening)

> Appended 2026-07-12 (apply-session burst 2); nothing above this line changed.
> Corpus-2 twin record: private development working note, not included.

**Propagation (§2 "pending") — DONE.** Burst 1 (l1-l5 commit `19f86f7`): notation
keys in `factory/design/ARCHITECTURE.md` §1 + `factory/operational/shared/runtime-and-model-map.md`,
19 first-use annotations; corpus-1 key in concept §15. Burst 2 (this session):
the rulings below cascaded into both corpora; model swap GPT-5.5 → GPT-5.6 Sol
across the l1-l5 living docs.

**Notation rulings (user, 2026-07-12).** Governing principle: notation covers
standing per-level seat families only; function-scoped auxiliaries stay named.

1. `test_author` — parked; no notation while it remains an L5 purpose-tagged
   child (notation decided in the tester-seat founding pass).
2. The four review-check seats — sub-seats of the owning Ln+ gate; no notation.
3. `#review` — at a level-n node, the Ln+ function-owner; address suffix and
   seat notation remain two distinct syntaxes (correspondence documented once,
   where addressing is defined).
4. L1& / L2& — real: L1& = intake/intent-spec production (formalizes the
   K45/M50 parallel-session offload); L2& = the design-cycle architect,
   collapsing at gate-PASS. Threshold-gated separate agents; design pass owed
   (l1-l5 deferred register).
5. Walking-skeleton spike — outside the taxonomy (an address, not a seat).
6. optimizer-L1 — name retired; the function lives with the principal
   coordinator (this project). Dated annotations at its l1-l5 mention sites.
7. First-class tester seat — founded (GPT-5.6 Sol / Codex); founding design
   pass owed; its notation is decided there.
8. Plan-alignment gate — **L/+**, the transition/cross-level review seat;
   **LE+** is the sanctioned substitute wherever the slash would collide with
   address-path syntax. Its auxiliaries (two reconstruction windows, the
   adversarial comparator, the specificity and coherence reviewers) are
   gate-scoped sub-seats under L/+ (per 2/3).

**Launch surfaces:** notation tokens stay on agent-visible surfaces; each
affected L2/L3 surface carries a one-sentence functional definition alongside
its token (accretion accepted as tiny).
