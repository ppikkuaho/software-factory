# Composition Gate & Foundations Riders — 2026-07-12

> **Status:** designed + user-ruled 2026-07-12 (same-day session; proposals
> CG-P1–P6 and P7–P9 → inline rulings). Feeds foundations items 1–3 and 7
> (macro note §9.9). Closes the "composition gate detail" open item.

## 1. Composition gate

### Seat (CG-P1) ✅
Fresh-spawn per review, stateless (verifier pattern), judgment-class Claude
seat. One seat, two stages. It **gates, never steers**; only the user
overrules it. **Stuck-case escalation (user ruling):** any review the gate
cannot resolve under its rules — missing inputs, contradictory records, a
situation its rule set doesn't cover — produces an explicit
`escalate-stuck` verdict naming what is missing, into the user queue
(queue-and-continue: holds only its own merge). The gate never silently
parks a merge.

### Stage 1 — mechanical screen (CG-P2) ✅ (minimal-effective config, user-ruled)
Runs on every merge that passed its lane gate. Five checks, all computed from
existing records: (a) scope overlap vs last **K=5** merged + all pending
(axes: lane, seats, actor-visible surfaces, file globs); (b) surface budget
per touched actor-visible surface (diff size + cumulative directive count vs
threshold); (c) settlement completeness across lanes; (d) queue adjacency
(>1 pending, or shared surfaces); (e) watch debt (open disconfirmations on
related surfaces). All green → land, screen logged on the merge record. Any
flag → stage 2. **Config posture:** thresholds provisional and coarse; no
additional metrics until observed pressure — minimal effective and safe.

### Stage 2 — elevated review (CG-P3) ✅ (non-overengineered, user-ruled)
Packet = existing artifacts only, no new instrumentation: candidate report +
granted claims + lane verdict; effective surfaces before/after; last-5 merge
records with watch outcomes; open settlement records; pending queue + scopes;
relevant spine entries. Verdicts (compositional only): **land /
land-after-X / consolidate-first / bounce-for-surface-rework / hold (named
unblock condition) / escalate-to-user / escalate-stuck**. Rails: no
re-adjudication of claim tiers; no research redirection; observations enter
the ledger through the normal proposal channel.

### Pre-set decision rules (CG-P4) ✅
Auto-resolutions, all logged: overlap on *different* surfaces with no
semantic conflict → land-after, older first · budget flag but merge is
directive-neutral-or-negative → land · settlement incomplete → hold until
assessments recorded (mechanical unblock) · 3+ pending → consolidate-first
proposal to the PC · watch-debt → hold only if tagged *regression*, else
land with annotation. **Escalate-to-user reserved for:** actor-visible /
pattern-scope guidance promotion; genuine directive-budget growth on core
surfaces with no consolidation path; gate-vs-PC disagreement. Plus
`escalate-stuck` per CG-P1.

### Authority + audit (CG-P5) ✅
Full authority from merge one (standing ruling). Every verdict is a logged
decision citing its screen outputs — auditable, observatory-watchable. No
bounce cap (merges rare; resubmission re-screens).

### Build shape (CG-P6) ✅
Screen = mechanical code over existing records. Stage 2 = versioned prompt
template driving a fresh seat (digest-generation mechanism). Methodology-lane
artifact; template changes epoch-gated.

## 2. Foundations riders

### P7 — Digest v2 template ✅
Four sections, nothing else: (1) header line (run id, actors, outcome,
tokens, wall-clock); (2) flow map (compact text diagram: actors/phases,
handoffs, breaks, loop/bounce counts); (3) per-actor play-by-play —
exploration (reasonable/efficient judgment + its computed basis), operations
(tests/code/changes + runs + results), closedown; (4) footer (provenance
hashes + epistemic labels, one line each). Anchors inline (`[main 12]`).
No preamble / overview / notable-behaviors sections (RQ-2 ruling).

### P8 — Spine derivation ✅
Derivation dispatch reads L4's governing docs → candidate expectations, each
carrying: statement · source citation (doc §) · observable trace signal ·
check-cost tier · class (**floor** = binary / **aspiration** = graded) ·
doc-version stamp. User ratifies an **active set capped at ~10** for L4 v1;
remainder = reference catalogue (rotation by lane focus).
Improvement-opportunity notes queue at the **user gate**; a launchd job
posts the daily macOS notification while the queue is non-empty. Check
prompts carry the neutral-framing rule verbatim (absence of suggestions is a
fine outcome; no quota).

### P9 — Role-packet v1 template ✅ (+ style-review ruling)
Five sections: (1) role & purpose (why the seat exists, what the system gets
from it); (2) position (who it serves, what depends on its output); (3) the
work (directive + calibrated guidance); (4) boundaries with rationale
(what's outside scope, whose job it is, why the separation exists); (5)
calibration. Shared duties = versioned blocks rendered per packet (L1-5
doc-system machinery, ported). Design-note citations live in frontmatter
only; agent-visible prose stands alone. **User ruling:** the L1-5 role
descriptions are the house best practice — a subagent style-review compares
the packets against them and keeps the same style where applicable.

## 3. Decision log

**User rulings (2026-07-12):** CG-P1 approved + stuck-case escalation
mechanism required. CG-P2/P3 approved with the explicit posture: minimal,
effective, safe — non-overengineered. CG-P4, CG-P5, CG-P6 approved. P7, P8,
P9 approved; P9 carries the L1-5-style subagent review. Run-brief
preparation: planning + multi-lens review agents over the day's corpus
before dispatch (executed same session).
