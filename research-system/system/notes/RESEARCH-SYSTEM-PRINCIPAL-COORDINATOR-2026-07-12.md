# Hypothesis Tree Research System — Principal Coordinator & Issue Object

> **Status:** designed 2026-07-12 in-session (P1–P10 proposal → user rulings,
> same day as the macro-architecture note). Concept-level; feeds foundations
> items 5–6 (macro note §9.9). This seat **names and realizes the macro note's
> "Tier-1 agent."** Build-level detail open where marked 🔲.

---

## 1. The seat: Principal Coordinator (PC) ✅

Name: **principal coordinator** (user ruling; supersedes "T1 director" working
name). Mission: the improvement harness's coordination and identification
seat — reads whole-system state, mints and drives **issues**, routes sub-goals
to lanes, schedules merges, arbitrates settlement, keeps the user queue
honest. Write domain: **navigation/coordination only** — never authors
epistemic content; its views are generated and uncitable.

**Context economy (user-ruled posture).** The PC is a *thin router*, not a
workhorse — if it does heavy work itself, its coordination degrades as context
fills. Operating shape: something comes in → **brief triage from numbers and
general system knowledge** → route forward / prioritize / drop. It works off
surfaces and readouts, never transcripts. Deeper analysis is delegated —
growth path: **dedicated subagents per function** (analytics, queue hygiene,
settlement prep) to keep the PC's own context lean.

**Fresh-context-per-wake (drafter call, resolves the context worry
structurally):** because all PC state is externalized (§7), each wake can read
state fresh — the seat is stateless-resumable by design, so context fill
*cannot* accumulate across events unless we choose to let it. Long-lived
context becomes an optimization, never a dependency (same doctrine as the
main system).

## 2. Surfaces ✅ (P2 as proposed)

Union ledger view · tree indexes/standings (top levels; descend only where
uncertain) · statistics readout + monitor rollups with co-located
interpretation rules · observatory report cards · issue queue + decision log ·
pending user-gate queue. **No raw material, ever** — no transcripts, archives,
or primary evidence; verified outputs and digests only.

## 3. The issue object ✅ (user: keep it minimal)

Tier-1 work atom. **One `active` at a time** (capacity-1 per macro §9.3;
prompt-enforced v1; object `ht`-written). Minimal schema — "not longer than it
has to be":

- `id` (global) · `title` · `provenance` (ledger-entry / observatory-finding /
  user-seed refs) · `scope` (implicated seats; LCA-docked) · `question` +
  **ex-ante done-definition** (achievement-based, declared at activation) ·
  `lanes` · `subgoals/dispatch refs` · `status`
  (`proposed → ratified → active → closed | parked | withdrawn`) · closure
  record (one paragraph + refs).

## 4. Routing and wake ✅ direction / 🔲 mechanics

The PC never dispatches research units. Sub-goals go to the relevant **level
box** (e.g. L2), which decomposes/passes forward; work lands in the relevant
**director's inbox**, and the inbox is a **hook that launches/spawns** the
director — invocation as imperative/command, not polling. Invocation mechanics
**borrowed from the L1-5 harness** (spawn chokepoint, launch-packet
composition, wake contract) — Phase-3 runtime detail 🔲; the direction is
ruled.

## 5. Decision loop ✅ (per wake)

Ingest deltas (ledger, report cards, statistics, monitor alerts, settlement
flags, user seeds) → update issue queue (draft/merge/re-rank; logged) → if no
active issue, propose next activation → for the active issue: read lane
reports, adjust sub-goals, detect done-definition-met → close + settle →
merge scheduling + composition-gate escalations → user-queue upkeep incl. the
daily pending-gate notification. Every steering step is a logged decision —
recorded, contradictable, and the calibration corpus for the future
autonomous monitor.

## 6. Sign-off → autonomy ✅ (user-ruled protocol)

Phase 1: **spawn the session; the user signs off on its decisions.** Phase 2,
after observed reliability: **tune it to run on its own** — with standing
observability throughout: the decision log plus the same trace machinery as
everywhere else (PC sessions are themselves observable runs; the observatory
can watch the coordinator like any other actor). User-tier acts stay queued
regardless of phase: issue activation (while in sign-off mode), composition-
gate escalations, pattern-scope claims, actor-visible promotions, spine
changes, improvement-note admissions.

## 7. State objects ✅ concept / 🔲 metric definitions

**Authoritative, PC-written:** the issue queue, the decision log (append-only),
closure records. Nothing else — trees stay lane-owned, epistemics stay
verifier-owned.

**The composed tree (user-ruled addition):** a whole-system tree composed of
all lane trees — the bigger picture in one object. V1 realization (drafter
call): a **generated projection** over the lane trees — uncitable view, same
standing as all generated views — with the growth path being concept §3.4's
one-system-tree (lanes as top-level branches) if/when it earns first-class
status. This keeps single-writer authority intact while giving the PC and the
user the composite picture from day one.

**The loop-effectiveness readout (user-ruled: the research/proof angle).**
The system must be able to **measure whether the self-improvement loop
works** — justify itself. Standing readout (metric definitions 🔲), sketch:
per-merge predicted-vs-observed effect (post-merge watches) · confirmed-
improvement rate · time-to-verdict per hypothesis · standing movement per
lane per epoch · spend per verdict. Rail (drafter): these are **evidence for
judgment, never targets** — the existing signals-not-verdicts doctrine applies
with full force, or the loop starts optimizing its own scoreboard.

## 8. Spawn ✅

Resumable bus seat (working name `ht-pc` 🔲), woken by events; plus an
**explicit spawn command** (user requirement) — one command that boots the
seat with its packet and surfaces. Foundations item-6 deliverable.

## 9. First activation = commissioning run ✅ (user-ruled framing)

The first issue (the L4 starter material, per macro rulings) runs as a
**flow/pressure test**: water through the pipes to find the leaks — explicitly
for that purpose. Success = the full stack exercised end-to-end (issue →
sub-goal → lane director inbox → dispatch → seam → report card → closure) with
**every break surfaced and documented**; the research outcome is secondary.
(Echoes the L1-5 commissioning method: mechanical trace-through before trusted
operation.)

---

## 10. Decision log

### User rulings (2026-07-12, this session)

- Name: **principal coordinator**.
- Thin-router posture: brief numbers-based triage, route forward; surfaces not
  transcripts; dedicated subagents per function as the context-management
  growth path.
- Issue object minimal — no longer than it has to be.
- Routing: PC → level box → director inbox; **inbox = hook that
  launches/spawns** (imperative); borrow invocation mechanics from the L1-5
  system.
- Decision loop as proposed (P5).
- Sign-off protocol: user signs off on decisions first; autonomy after proven;
  observability (decisions/traces) standing either way, same machinery as
  everywhere.
- State: **composed tree** over all lane trees wanted for the bigger picture;
  **loop-effectiveness measurement** wanted (research/proof angle — justify
  the system).
- Explicit spawn command required.
- First run is a **pressure test** — purpose is finding leaks, not the
  research result.

### Drafter judgment calls — ALL CONFIRMED by user, same session (2026-07-12)

- **Fresh-context-per-wake** as the structural answer to the context-fill
  worry (state externalized → stateless-resumable seat).
- Composed tree realized as a **generated projection** in v1 (authority stays
  with lane trees); §3.4 one-system-tree is the promotion path.
- Effectiveness-readout metric sketch (§7) + the metrics-are-evidence-never-
  targets rail.
- User-tier act list in §6 (carried from the macro note's standing rules).
