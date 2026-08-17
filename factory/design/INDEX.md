# Software Factory — the factory's design doc set

**This is the build doc set.** Point the build (and any agent) at the files listed ACTIVE here. Anything not listed is outside this curated snapshot.

> **MAINTENANCE RULE (do not skip):** adding or superseding a design/build doc **updates this index in the same change.** A doc that exists on disk but is not listed here is invisible to anyone — human or agent — who trusts the map. That is exactly how the four cluster specs got "lost" by the 2026-06-05 completeness audit: they were authored after this index and never added to it. Full write rules (schema, supersession, burst-end ritual): **`DOC-PROTOCOL.md`**.

## Canon-precedence
1. `SUBSTRATE-PHYSICS.md` and the living specs' dated current-contract overlays are the current source of truth for substrate behavior.
2. The four **runtime cluster specs** retain their derivation and remain canonical where their current overlays do not supersede them.
3. The older Jun-02 design specs are canonical for the semantics they cover, after any dated truth-status or current-contract banner in that document.

## Builder reading order
1. `SUBSTRATE-PHYSICS.md` (one-sitting current model)
2. The **runtime cluster specs**: `DAEMON.md` ① → `WATCHDOG.md` ② → `TRANSPORTS.md` ③ → `SCALE.md` ④
3. `harnessd/IMPLEMENTATION-PLAN.md` (current architecture overlay + historical increment record)
4. `ARCHITECTURE.md` (semantic spine) + the older specs (`OBSERVABILITY`, `COMMUNICATION`, `QUALITY-GATE`, `WORKSPACE-SCHEMA`, `PLAN-ALIGNMENT-GATE`, `DECOMPOSITION-METHODOLOGY`)
5. `operational/` (the agent-facing runtime docs)

## ACTIVE — runtime cluster specs (`design/`) — the build-defining design
- **`SUBSTRATE-PHYSICS.md`** — the one-sitting current substrate brief: run-scoped lifecycle,
  gateway, notary, messages/questions, wake table, barriers, turn state, jail, and promotion. It
  distinguishes implemented physics from the owed generated journey view and keeps the proposed
  laws/service taxonomy unratified.
- **`DAEMON.md`** ① — harness-as-a-process: daemon, single-writer executor, binding ledger + intent-first WAL, reconcile, claim-before-spawn chokepoint, genesis, fencing, and optional run-scoped L3 module-panel commissioning on the existing start request/root binding.
- **`WATCHDOG.md`** ② — liveness & lifecycle: hook-primary turn state and deterministic turn-end
  return checking, with process-death, hung-tool, and hook-less-runtime evidence fallback; preserves
  coordinator recovery, fencing, and reviewer recovery.
- **`TRANSPORTS.md`** ③ — durable sender-owned messages/open questions, verified event wakes,
  cohort barriers, compatibility shims, and the user-authorized human channels.
- **`SCALE.md`** ④ — admission gate, per-runtime ceilings, OAuth-subscription usage model (mostly deferred-with-triggers).
- **`SECURITY.md`** — containment floor (cross-cutting): the `sandbox-exec` write/read-jail, secret protection, skip-perms-in-jail posture, fleet HALT. Wires into the spawn chokepoint (the seatbelt is the pane launch command). Decisions locked 2026-06-05 (Option A).
- **`ROLE-RESOLUTION.md`** — how role/prompt resolution works at boot (cross-cutting): Claude seats load a per-spawn composed `.identity-prompt.md` whose base is `operational/shared/system-prompt.md` plus the launch/identity surface; Codex seats keep native instructions and receive harness startup material through the boot prompt/native-instructions sentinel; broader protocol docs remain read-in-place references. Created 2026-06-05; amended by the 2026-06-12 identity auto-load ruling.
- **`INTAKE-TO-DELIVERY.md`** — the end-to-end application arc (cross-cutting): user request → L1 intake/intent-spec → project genesis → L2 spawn → execution → L1 preliminary fidelity playback → owner-final CONFIRM → deliberate **control-plane promotion** of the product out of the gitignored `/runtime/` to the intake-captured destination (the ONE sanctioned cross-write-jail action, never an agent write). An explicitly commissioned operator delegate has distinct answer/WAL/view/promotion labels and is never presented as owner. Rides the substrate; the delivery build code began in IMPL-PLAN Increment 17 and the owner-final gate landed in Q6. Closes register V3. Created 2026-06-05; amended 2026-07-24.
- **`BEHAVIOURAL-VALIDATION.md`** — Phase 6, the trace-through (cross-cutting): validates the system's DESIRED BEHAVIOUR (the agents, in-situ, doing what they're meant to with the pieces they need) — distinct from the Phase-5 mechanical verification of the substrate. A leak = the flow silently routing AROUND a gap. Two instruments: pieces-present (deterministic, front-loaded) + behaviour-per-joint evals (real agents) + the full trace (commissioning). Behavioural contracts are the user's to set. IMPL-PLAN Phase 6 (Inc 18–24). Created 2026-06-06.
- **`DOC-SYSTEM.md`** — the documentation-management system (cross-cutting): shared cross-level duties as versioned blocks (`operational/shared/blocks/` + `registry.json`) rendered into the role docs between markers; idempotent render/check tool (`tools/render_blocks.py`) + checker test (`tests/test_doc_blocks.py`) failing red on omission/drift; standard-artifact templates (`operational/shared/templates/`); per-node file schemas (extension in `WORKSPACE-SCHEMA.md`, EVAL-side only — never a runtime gate). **A green checker = mechanical conformance only; doc HEALTH belongs to the judgment layer (run-adherence audit).** Created 2026-06-12 from the Run-2 coverage findings.
- **`DOC-PROTOCOL.md`** — the repo's documentation schema + write protocol (cross-cutting, governs THIS doc set rather than the runtime): what lives where, same-change indexing, supersession-by-banner, the burst-end ritual, amendment protocol. Delivered to every session via the root `AGENTS.md`/`CLAUDE.md` pair (`CLAUDE.md` is a symlink of `AGENTS.md`). Created 2026-07-07 from the estate audit (`working-notes/ESTATE-AUDIT-2026-07-06.md`).
- **`GATE-LIFECYCLE.md`** — the gate-owned-forwarding plan (cross-cutting): executor output is only a candidate; review gates own parent-visible completion. Defines the gate state model, review packet, default upper-gate four-axis review decomposition plus the commissioned L3 module-panel experiment, per-level gate contracts, harness enforcement spine, implementation increments, passive observability path, and behavioural tuning readiness criteria. Created 2026-06-15.
- **`HIGHER-LEVEL-GATES.md`** — the L4+/L3+/L2+ portfolio-gate behavior contracts: review altitude, review packets, the default four-seat L3+/L4+ and five-seat L2+ rosters, optional ordered L3 module arms including the broad unscoped reviewer, artifact schemas, PASS/BOUNCE/ESCALATE semantics, bounce scope, product-probe isolation/calibration, and deterministic prerequisites. Specializes the gate lifecycle for composition gates above L5+ without cloning L5+ code-review behavior upward. Created 2026-06-15.

*Internally consistent post cross-cluster reconciliation (2026-06-05).*

## ACTIVE — the build (`harnessd/`)
- **`harnessd/IMPLEMENTATION-PLAN.md`** — current module/disk architecture plus the historical
  ordered **0–16 build-increment record** (Phase 5a). OAuth-subscription-only invariant. Code lands
  in `harnessd/`; per-build trees live in **`~/l1-l5-workspaces/`** outside the repo.

## ACTIVE — older design specs (`design/`)
`ARCHITECTURE`, `DESIGN-PRINCIPLES`, `DECOMPOSITION-METHODOLOGY`, `COMMUNICATION`, `OBSERVABILITY`, `WORKSPACE-SCHEMA`, `PLAN-ALIGNMENT-GATE`, `PROJECT-PLANNING`, `QUALITY-GATE`, `IMPROVEMENT-WORKSPACE`, `VISION`, `BEHAVIOURAL-RUN-LOG` (run-observation template). **Phase-0 reconciliation: DONE.**

## ACTIVE — agent-facing runtime docs (`operational/`) — what spawned agents load
`operational/L1..L5/{role,config,soul,spawn-template}.md` (+ L1 handbook/intake/skills, L3 planning-template, L5 swe-handbook) and `operational/shared/{system-prompt, agent-lifecycle, comms-protocol, git-protocol, runtime-and-model-map, agent-definition-principles, intent-spec-contract, review-handbook, user-profile-schema}.md`.
- **`operational/shared/system-prompt.md`** — shared minimal Claude prompt base, not the exact file passed at every spawn. Claude seats receive it at the head of the composed `.identity-prompt.md`; Codex seats use native instructions and record the native-instructions sentinel. Promoted 2026-06-05; `ROLE-RESOLUTION.md` governs mechanics.
- **`operational/shared/review-handbook.md`** — agent-facing operating contract for review gates: `#review` as orchestrator/verdict owner, the four-seat L3+/L4+ and five-seat L2+ upper-gate rosters, REVIEW-CHECK launch surfaces, product-probe charter boundaries, grounding material, finding schema, and gate-artifact output contract. Created 2026-06-15.
> **ROLE-RESOLUTION (DONE, 2026-06-05; amended 2026-06-12):** the boot model is reconciled. Claude-Code seats receive a composed `.identity-prompt.md` (shared prompt base + launch/identity surface), Codex seats keep native instructions and get explicit boot-prompt first-reads, and broader protocol docs remain read-in-place references. **`design/ROLE-RESOLUTION.md`** is canonical for role/prompt resolution and supersedes the old bare `--system-prompt-file` model.

## Records & archives
Pinned Claude Code substrate: `PINNED-CC.md`. *This is a curated public snapshot. The dated session notes, bridges, and run logs under `design/working-notes/`, the recovered prior art under `research/`, and the development changelog under `dev/` are not part of it. Citations to those paths in the specs below are provenance markers — they record where a ruling was made, not links to follow.*
