# Intent-Spec Contract (canonical)

The **intent-spec** is the founding artifact of every project: the tagged, hierarchically-IDed record of what the user actually needs, produced by the structured intake (Phase 1) and guarded by L1 for the project's life. It is the single source from which the entire downstream traceability spine descends — IDs are minted **only here** (PROJECT-PLANNING.md §"One Spine"). Everything below either traces to an intent-spec ID or is sanctioned scope.

This document is the **canonical contract**: the observable, checkable definition of what a well-formed intent-spec must contain. The dry-run's `dry-run/intent-spec.md` is the **reference example** that satisfies this contract — read it as the worked instance of the schema below.

## On "SDD"

Earlier intake prose said the grilling session "produces the SDD or equivalent." **This was a misnomer and is retired at the intake boundary.** "SDD" (Spec-Driven Development) is the *fidelity-spine methodology that threads the whole cascade* (DECOMPOSITION-METHODOLOGY.md §4), not a single intake deliverable. The artifact the intake produces is **the intent-spec** defined here — the topmost spec on that spine. Wherever intake docs said "SDD or equivalent," read **"the intent-spec."** (The SDD spine continues to exist; the intent-spec is its first link.)

---

## The return contract — what the intent-spec MUST contain

A checker (the gate's Check 0/1, or L1 ingesting the grilling session's return) treats an intent-spec as **valid** iff all of the following are present and well-formed. A missing or malformed element is a return-contract violation: the grilling session has not finished, and L1 must not ingest it as the signed brief.

### 1. Outcomes

One machine-readable table of concrete success statements **in the user's own terms**. Every row
carries a stable dotted-hierarchical `Outcome ID` (`O-001`, `O-001.1`, …), the outcome text, and
the verbatim intent span(s) that ground it. Outcome IDs are unique inside the project and form the
final fidelity playback roster; L1 records one recipient-visible observation per live outcome.
Outcomes are the roots of the requirement tree; every requirement names a parent outcome.
Observable: ≥1 outcome; each outcome cites ≥1 verbatim user span.

### 2. Hierarchically-IDed requirements table

One row per requirement, IDs dotted-hierarchical (`R-007`, `R-007.1`), each row carrying **all** of:

| Field | Contract |
|-------|----------|
| `ID` | Dotted-hierarchical; child resolves to parent by prefix truncation; no duplicates, no gaps in the address spine. |
| `Requirement` | The requirement text. |
| `Tag` | Exactly one of `decided` / `delegated` / `deferred`. Well-formedness is a hard gate check (Check 1). |
| `Priority` | The requirement's weight, or inheritance from parent outcome. Carries the user's priority overrides. |
| `MNF` | Must-never-fail flag (`YES` / `—`). Any `YES` row MUST be decomposed in §4. |
| `Parent` | Parent outcome or parent requirement ID. `DR-` derived requirements carry a **`serves`** link instead (the intent ID they serve), never a parent. |
| `Fluency` | `technical` or `plain` — the per-element render-depth that drives the gate's M58 render decision for anything keyed to this ID. |
| `Reflect-back status` | `pending` until the user confirms it at reflect-back (or at Phase 3 for L1-derived), then `confirmed`. **This field is the producer of the stamp the gate's Check 1 reads to forbid freezing on unconfirmed foundations.** A requirement with `pending` status is observable in the trace and blocks freeze of any interface depending on it. |

### 2a. Intent Journeys

One machine-readable row per recipient-visible journey the finished product must support. Journeys
are first-class load-bearing confirmations: the grilling session elicits them, the reflect-back
plays them to the user, and only `confirmed` rows freeze. They are the sole journey authority for
the L2+ user-simulation probe; reviewers never infer a journey from Outcomes prose.

| Field | Contract |
|-------|----------|
| `Journey ID` | Stable `J-NNN` dotted-hierarchical identifier, unique inside the project. |
| `Kind` | Exactly `positive` or `negative`. |
| `Requirement IDs` | One or more live `decided` / `delegated` requirement IDs this journey exercises. |
| `Starting state` | Concrete externally relevant precondition. |
| `Action through the face` | What the user/operator does through the documented artifact face. |
| `Observable result` | The recipient-visible result or refusal. |
| `MNF obligation` | `—` or one exact MNF requirement/child ID exercised by the journey. |
| `Reflect-back status` | `pending` until the user confirms this journey, then `confirmed`. |

At least one journey row is required. Every referenced requirement must be live; a journey cannot
smuggle a deferred obligation into current scope. Negative and MNF journeys name the failure or
safe-refusal behavior explicitly. A missing table is a contract violation, not permission for a
later seat to guess journeys from narrative text.

### 3. Per-area opinionated/delegated + technical-fluency map

A table, one row per area surfaced during tradeoff-probing, with: `Opinionated?` (yes-fixed / yes-directional / yes-absolute / no-delegated), `User fluency here`, `Render at gate as` (`technical` / `plain` — the M58 input), and `Evidence` (which tradeoff fork the user engaged, by probe ID). Plus the **tradeoff probes run**, logged by ID (`T-1`, `T-2`, …) with the fork posed and the answer. This is the elicitation record, not a survey — each row must cite the fork that produced it.

### 4. Must-never-fail decomposition

Every `MNF: YES` requirement decomposed into **atomic, individually-testable obligations**, each row carrying:

| Field | Contract |
|-------|----------|
| `Obligation ID` | A child ID under the MNF (`R-007.1`). |
| `The concrete failure it forbids` | The specific bad outcome, stated as a behavior — not a topic. |
| `Why it is its own obligation` | The distinct mechanism that defeats it (so a reviewer sees these are independent failure modes, not one test). |
| `The negative test the gate will require` | The **failure-path test** stated concretely enough that Check 2 can confirm presence and Check 4b can confirm adequacy. An MNF obligation whose described test is vacuous/tautological is a gate FAIL. |

Plus the **cross-cutting confirmation**: the argument that the obligations are independent failure modes (why the MNF can't be one test), and any **safe-failure direction** the user confirmed (which way the system fails when a guard trips). The user confirms **the decomposition itself** at reflect-back, not just the headline.

### 5. ID → intent-span map (verbatim, with derivation flags)

One row per minted ID giving the **verbatim user span(s)** it claims to carry, and a `Derived?` flag. Rows marked **`[L1-derived]`** are NOT verbatim user words — they are professional-judgment requirements L1 surfaced from the user's stated constraints. **These are ordinary root `R-NNN` requirements flagged `[L1-derived]` in this map — NOT `DR-` rows.** `DR-` is reserved strictly for derived requirements born **below** intake (carrying a serves-link, per PLAN-ALIGNMENT-GATE.md §"Requirements Traceability"); a requirement surfaced AT intake is a root `R-NNN` regardless of whether the user spoke its exact words. The `[L1-derived]` flag (not a `DR-` prefix) is what marks it as L1's judgment rather than the user's verbatim words. This map makes the prose→ID minting **inspectable** at the gate (Check 0, atomization completeness, reads this side-by-side with the raw prose). Every L1-derived requirement is flagged here so the gate and the user can confirm or reject it before it hardens from `delegated` to `decided`.

### 6. Trace-block per requirement (coordinated with the trace-block spec)

Each requirement carries the **same per-element trace-block** every downstream level emits (PLAN-ALIGNMENT-GATE.md §"Requirements Traceability"; OBSERVABILITY.md). At the intake level the trace-block is a **per-element `trace:` stanza adjacent to each requirement** — the trailing `trace` column cell on that requirement's table row (or an inline stanza on the row), `kind: requirement`, `level: L1` — asserting the requirement's parent/serves ID and its intent-span. It is **not whole-file front-matter**: there is one stanza per requirement, attached to that requirement, never a single file-level `intent_ids:` header. These root stanzas are the **root** of the flow-down chain that L2 (modules), planning-L3 (L3&; design elements), L4 (tasks), and the L4-tester (acceptance tests) extend. The RTM-builder harvests these intake trace-blocks together with every downstream one and the ID→intent-span map (§5) to generate the RTM by construction. Observable: every requirement row carries its own parseable `trace` cell/stanza (a missing one is a Check 1 hard FAIL), and its asserted parent resolves by prefix truncation.

### 7. Reflect-back script + confirmation status

The plain-language playback L1 reads to the user, mapping each claim to its IDs, and a **confirmation status** field. Load-bearing confirmations (every intent journey, the MNF decomposition, any safe-failure direction, every `[L1-derived]` requirement) are called out as the items that remain `pending`/L1-derived assumptions until the user confirms them.

### 8. Delivery destination

The single resolvable destination where the finished product is delivered, captured **at intake** alongside the rest of the brief. Two fields:

| Field | Contract |
|-------|----------|
| `Destination` | A single resolvable target — a user-path (e.g. `~/Projects/foo`) or a git remote — outside the runtime tree. Exactly one. Write it as a bare machine-usable value whenever possible. |
| `Kind` | Exactly one of `filesystem-path` / `git-remote`. Drives how the control plane promotes (copy-out vs push). |

This is the captured target for deliberate control-plane promotion after the current owner-final playback CONFIRM (or distinctly labelled commissioned-delegate CONFIRM) — never an agent-writable path (agents are jailed to the `/runtime/` node subtree; the destination lies outside every jail). Observable: present and non-empty for any project intended to ship. A project the user wants to keep in place with no external delivery MUST mark this **explicitly** as `in-place / no external delivery` — an absent destination is a return-contract violation, not an implied no-op.

---

## What this contract enables downstream (why each field exists)

- **Hierarchical IDs + trace-blocks** → the one-spine RTM, generated not maintained.
- **Confirmed intent journeys** → the deterministic L2+ user-simulation roster; no reviewer invents a user path from prose.
- **Tags + reflect-back status** → the gate's Check 1 forbids freezing on `pending` foundations; the build cycle inherits only `confirmed` requirements.
- **Per-area fluency** → M58 intake-calibrated render-depth: the gate shows fluent areas the technical claim and delegated areas the plain-language implication.
- **MNF decomposition + negative tests** → Check 2 (presence of failure-path test) and Check 4b (adequacy of the test's described mechanism).
- **ID→span map + `[L1-derived]` flags** → Check 0 atomization completeness can audit the prose→ID translation instead of treating it as axiomatic.
- **Delivery destination** → the captured target the control plane promotes the finished product to only after L1's complete preliminary fidelity playback receives the current owner-final CONFIRM (or distinctly labelled commissioned-delegate CONFIRM) and L1 deliberately invokes promotion — the one gated cross-jail write out of `/runtime/` (see `design/INTAKE-TO-DELIVERY.md`).

---

## Ownership and edit policy

- **Owner:** L1. L1 **owns and guards** the intent-spec — it is L1's deliverable and L1 is accountable for it. (Producing the heavy elicitation that fills it is dispatched to the grilling session; owning the result ≠ producing it inline — see `operational/L1/role.md` §"Owns the spec, dispatches the elicitation.")
- **Edit policy:** living during intake; **frozen as the signed brief** at reflect-back confirmation. After freeze, changes only via an explicit **intent-revision record** — nothing downstream overrides the intent-spec silently.
- **Location:** `projects/{project-name}/client-brief/intent-spec.md`.

---

*Created: 2026-06-02 — canonical intent-spec contract. Formalizes the artifact whose reference instance is `dry-run/intent-spec.md`; retires "SDD" at the intake boundary in favor of "the intent-spec." Consumed by `operational/L1/intake-session-template.md` (output contract), the plan-alignment gate (Checks 0/1/2/4b), and `design/INTAKE-TO-DELIVERY.md` (delivery-destination → control-plane promotion).*
