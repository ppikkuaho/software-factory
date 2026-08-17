# INTAKE → DELIVERY — The End-to-End Arc

The canonical end-to-end path from a user request to a delivered product on the L1–L5 harness. This is the **application arc** that rides the substrate (the 0–16 build queue, `harnessd/IMPLEMENTATION-PLAN.md`); it is **not new substrate**. It connects pieces that already exist — the intake/grilling session, the intent-spec contract, the spawn chokepoint, the deliverable binding block, the write-jail — and names the one genuinely-new mechanism the full arc needs: **control-plane promotion** out of the gitignored `/runtime/` tree.

> **Status:** v1 design. The **full arc including promotion is v1** (user decision, 2026-06-05). Register provenance: closes the V3 register row (`working-notes/DEFERRED-REGISTER.md`) — *"Intake → L2 arc + product delivery / promotion-out-of-/runtime/."* Stages 0–5 ride the existing substrate plus one new intent-spec field (the delivery destination). The new build code is the **promotion increment** (§3), registered as a new IMPL-PLAN increment after the substrate (17+).
>
> **Two user decisions baked in (2026-06-05):** (i) the full arc incl. promotion is v1; (ii) the **delivery destination is captured AT INTAKE** — a user-path (e.g. `~/Projects/foo`) or a git remote — as a field of the intent-spec.

---

## 1. The Six-Stage Arc

Each stage points at the doc that owns it; this section connects them, it does not duplicate them.

### Stage 0 — Kickoff (human → L1)

The operator deliberately invokes the repository Claude Code skill's `start` action (underlying
verb: `harnessctl start --build-id … --intake-file …`). That one run-scoped command bootstraps
the daemon if absent, the daemon spawns/adopts L1 at the L1-root address (`DAEMON.md` §7), and the
operator attaches to L1's tmux session. L1 is interactive and has no parent agent. The initial
request is already present in L1's durable inbox before its actor opens; the attached conversation
continues reflect-back/intake directly with the live L1.

### Stage 1 — Intake → intent-spec

L1 preserves the exact initial intake bytes as `client-brief/raw-request.md`, then dispatches the
throwaway grilling session. The session returns only the finished intent-spec, not its conversation.
At reflect-back confirmation L1 freezes the raw request and intent-spec together. The former is the
surviving independent input to atomization; the latter is canonical intent.

### Stage 2 — Project genesis (L1 writes the project node)

On a confirmed intent-spec, L1 creates the project node and writes `client-brief/raw-request.md`,
`intent-spec.md`, `vision.md`, and `priorities.md`. The first two carry notary receipts and are
changeable only through explicit revision records; the views are immutable.

### Stage 3 — L1 spawns L2

L1 spawns L2 at the project node through the **single spawn chokepoint** (`DAEMON.md` §6, `role_variant=L2`), with a brief whose load-manifest **points at `client-brief/`** — read in place, **pointer-not-payload**, never copied. L2 reads `client-brief/vision.md` + `priorities.md` (`operational/L2/spawn-template.md`), produces the concept design (component map + interface contracts + ADRs + per-module specs), and drives realization through the coordinated planning round into the L3/L4/L5 cascade (`PROJECT-PLANNING.md`; the cluster specs own the mechanics — referenced here, not duplicated).

### Stage 4 — Execution → complete

The cascade builds the product **inside `/runtime/proj/{project}/`**, every agent write-jailed to its own node subtree (`SECURITY.md` §1.3). Completion flows **up** through the frozen contracts as terminal signals (`DAEMON.md` §3.6); each parent collapses and synthesizes its children's results. L2 runs the final cross-area integration review and **reports project-complete to L1**.

### Stage 5 — Final acceptance (L1)

> **Authority ruling (owner docket, 2026-07-24; supersedes the 2026-06-11 default):** L1's
> fidelity judgment is PRELIMINARY and the OWNER renders final accept through one durable playback
> question. A commissioning operator may act only through an explicit run-scoped delegation
> declared at launch; every answer, WAL row, view, and promotion response labels that authority
> `operator-delegate`, never owner. Owner authority remains live on a delegated run.

L1 judges the assembled result against the **frozen intent-spec** — the anchor it has guarded
since Stage 1. This is an **intent-fidelity** judgment, not a re-do of technical review. L1 drives
the recipient-visible face and writes `client-brief/fidelity-judgment.md` with
`Preliminary Verdict: accept|reject` plus one exact drive/observation/evidence row for every frozen
`O-*` outcome and every `MNF: YES` requirement. It then runs
`harnessctl fidelity-playback <project-node-address>`, which freezes one content-addressed
pointer-only question bound to the current intent and judgment hashes. The owner's **CONFIRM**
authorizes Stage 6; **REJECT** closes the question, wakes L1, and routes the stated reason through
one canonical message to the single live direct L2 project child. The intent-spec is the single
thing acceptance is measured against.

### Stage 6 — Promotion / delivery (control plane)

After the current playback question is **CONFIRMED**, L1 deliberately runs
**`harnessctl promote <project-node-address> --decision accept`**. Answering never promotes as a
side effect. The CLI serializes the request to the daemon, which verifies the preliminary artifact
and immutable confirmed answer before promoting the finished product OUT of the gitignored
`/runtime/` project node TO the intake-captured destination (§3). The daemon performs the
cross-jail copy/push and stamps the binding via `executor.deliver`; the CLI stays a client, the
executor stays the single writer, and promotion stays a harnessd action. After delivery,
**project teardown / reclaim** of the
`/runtime/` tree is a **deferred follow-on** (register D7), not part of v1 promotion.

---

## 2. Connectivity — the frozen intent-spec and its client-brief views

The arc has one canonical brief, one exact provenance artifact, and two derived views in
`client-brief/`:

- `intent-spec.md` — the **canonical frozen brief**. Owner L1; produced by the grilling session; frozen at reflect-back confirmation (`operational/shared/intent-spec-contract.md`). It is the topmost spec on the SDD fidelity spine and the source of every minted requirement ID. **Everything downstream traces to an intent-spec ID or is sanctioned scope.**
- `raw-request.md` — exact genesis intake bytes, frozen at the same confirmation boundary and used
  by the once-per-intent atomization audit.
- `vision.md` and `priorities.md` — **L1-authored VIEWS of the confirmed intent-spec**, written at project creation, **immutable** (`WORKSPACE-SCHEMA.md` §130–134). `vision.md` renders what is being built / for whom / what success looks like; `priorities.md` renders the opinionated-vs-delegated triage and the priority overrides that flow through the project.

L2 reads `vision.md` + `priorities.md` (`operational/L2/spawn-template.md`) — the **distilled views** — and pulls the canonical `intent-spec.md` on demand (pointer-not-payload). The relationship is strict: the **intent-spec is canonical**; the two views never override it, and after freeze the spec changes only via an explicit intent-revision record. This is the brief side of the arc; promotion (§3) is the delivery side.

---

## 3. Delivery — promotion is a control-plane cross-jail write

**Promotion is performed by `harnessd`, not by any agent, and it is gated on the current
owner-confirmed fidelity playback.**

Why it cannot be a jailed-agent write: every agent — L1 included — is **write-jailed to its own node subtree under `/runtime/`** (`SECURITY.md` §1.3; the seatbelt profile is a global `(deny file-write*)` then an allow-list scoped to the node's `WORKROOT`). The **delivery destination is outside every node's write-jail** — it is a user-path or a git remote, deliberately *not* under any node subtree. So crossing that boundary is **structurally impossible for a jailed agent**; it is a **control-plane operation**, performed by the daemon, which is in the trusted control plane (`SECURITY.md` §1.1) and is not itself write-jailed. This is the **one** sanctioned cross-jail write in the system, and it exists precisely because the write-jail invariant must hold for every agent.

The operation is **gated on L1's preliminary fidelity-judgment artifact plus the current immutable
CONFIRM answer** (Stage 5). The deliverable binding block tracks the surface it needs (`DAEMON.md` §3.2 /
`IMPLEMENTATION-PLAN.md` §3.4): `deliverable_state`
(`planned|active|waiting|completed|blocked|cancelled|delivered|delivery-failed`),
`stop_condition`, `write_targets` (the **in-jail work surface**), `evidence_refs`,
`acceptance_ref`, optional **`delivery_source`** (the in-jail product surface to ship; null means the
node workspace root), and the dedicated **`delivery_destination` + `delivery_kind`** — the
out-of-jail promotion target (mirroring intent-spec §8), kept **distinct from `write_targets`** so the
jail boundary stays legible. Before crossing the jail boundary, `harnessd.promote` requires
`client-brief/fidelity-judgment.md` (or the single project-subtree equivalent) with
`Preliminary Verdict: accept`, complete per-outcome/per-MNF evidence pointers, one current
content-addressed playback question, and a notary-current CONFIRM answer from the owner or the
explicitly predeclared commissioning delegate. Missing, stale, ambiguous, rejecting, or
wrong-authority surfaces hold the gate and leave `deliverable_state` untouched. When the frozen intent-spec carries §8, promote normalizes the
destination before use (including filesystem `~` expansion and markdown display annotations) and
treats that artifact row as authoritative over a stale cached binding destination. The promote-out
sets `deliverable_state=delivered` (or `delivery-failed` on a failed promote, plus the §6.3
escalation row) and writes the normalized target into `delivery_destination`. A failed promote
records the failed target; repair uses the same control-plane promote path with an explicit
destination override after `deliverable_state=delivery-failed`, instead of editing the frozen
intent-spec or live binding by hand. **The promote op is
BUILT** (`harnessd/promote.py`, Increment 17). Its
trigger is **`harnessctl promote <project-node-address> --decision accept`** — issued deliberately
by L1 after Stage-5 confirmation, serialized to the running daemon's IPC `promote` verb;
the **daemon** verifies the top fidelity gate, performs the cross-jail copy/push, and stamps the
binding via `executor.deliver` (the single writer). By default the source surface is the node's
**`nodes/<path>/` workspace** (`addressing.node_dir` — the one canonical address→workspace mapping
every agent writes); when the product lives below that root, L1/operator supplies
`--delivery-source <relative-product-dir>` (for example `build/app`). The daemon resolves that source
inside the promoted node workspace and refuses escape paths. §4's `/runtime/proj/{project}/`
phrasing refers to the **logical** path, physically rooted at `<RUNTIME_ROOT>/nodes/`. The
control-plane dotfiles inside the selected source (`.sign-off.*` / `.signal.*` / `.inbox.*`) are
harness machinery and are excluded from the promoted tree.

---

## 4. `/runtime/` vs `proj/` — the logical tree and its throwaway root

The logical `proj/{project}/…` tree (the one-spine workspace paths, `WORKSPACE-SCHEMA.md`) is rooted, **at runtime**, at the **gitignored throwaway `/runtime/`**: per-build project trees live under `/runtime/proj/{project}/` (`WORKSPACE-SCHEMA.md` §138–142; `IMPLEMENTATION-PLAN.md` — code in `harnessd/` is tracked, `/runtime/` is gitignored). Genesis spawns L1 at the L1-root; **projects are nodes below it**, so the project node L1 creates in Stage 2 sits inside L1's own subtree. Everything an agent produces — the whole project node and its deliverable — is written **inside `/runtime/`** and write-jailed there. A finished deliverable **does not stay in `/runtime/`**: promotion (§3) is what moves it out to the captured destination. The teardown/reclaim of the spent `/runtime/` tree afterward is deferred (D7).

---

## 5. Invariants Preserved

- **Write-jail.** Every agent is confined to its own `/runtime/` node subtree. **Promotion is the one control-plane cross-jail write**, gated on the current owner-confirmed fidelity playback, performed by `harnessd` — **never an agent write** (`SECURITY.md` §1.3).
- **OAuth-only.** All spawns boot on the OAuth subscription token; no API-key path (`SECURITY.md`, `DAEMON.md` §6).
- **The intent-spec is the frozen anchor.** Frozen at reflect-back confirmation, guarded by L1 for the project's life, and the single thing L1's preliminary judgment and the owner's final playback answer judge against (`operational/shared/intent-spec-contract.md`).
- **Pointer-not-payload.** Every brief points at `client-brief/` (and upstream intent), read in place — never copied (`WORKSPACE-SCHEMA.md`, `operational/L2/spawn-template.md`).

---

*Created: 2026-06-05 — canonical intake→delivery arc spec. Connects the intake/grilling session, the intent-spec contract (+ new delivery-destination field), the spawn chokepoint, the deliverable binding block, and the write-jail; names control-plane promotion as the v1-new mechanism (IMPL-PLAN increment 17+). Closes register V3. Referenced by `WORKSPACE-SCHEMA.md` §138–142.*
