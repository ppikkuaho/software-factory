"""Observatory — the field organ's on-demand per-run pass (design area C1).

Watches the *subject system only* (L1-L5 production runs). Consumes the trace
reader's Behavioral Record (evidence-grade, post-hoc) plus the mechanical trace
and — when present — the L1-L5 audit event log, and produces a bounded per-run
**report card** (C1 §8), merge-updates the mechanical **statistics store**, and
proposes ledger entries for the director's attention surface (C1 §6). It writes
NOTHING to the ledger itself — proposals only (verifier-gated).

Three-layer pass (C1 §3): L1 mechanical screens (no LLM, 100% of every run),
L2 triage (one `claude -p` over the digest + screens + spine), L3 deep dives
(`claude -p` per above-threshold flag). V1 ships ON-DEMAND (no daemon, no
L1-L5 run-completion hook — that install is a documented scope cut, handoff §4).
"""

OBSERVATORY_VERSION = "observatory/1.1.0"

__all__ = ["OBSERVATORY_VERSION"]
