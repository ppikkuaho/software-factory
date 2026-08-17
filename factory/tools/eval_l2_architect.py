"""L2 architect behavioural eval — synthetic intent-spec in, real jailed L2 produces the architecture.

Decoupled-level approach (user direction): instead of running the full upstream L1 intake, SYNTHESIZE a
clean intent-spec (the L1 output) via a one-shot LLM call grounded in the real contract, feed it to a
real jailed L2, and score L2's concept-design output (component map + interface contracts + ADRs).

Usage:  python3 tools/eval_l2_architect.py
Burns real model usage (the synthetic-spec generation + the L2 agent). Saves to runtime/eval-runs/.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import harnessd.eval.interactive_eval as ie

ROOT = Path(__file__).resolve().parents[1]

L2_MANIFEST = [
    "operational/L2/soul.md", "operational/L2/role.md", "operational/L2/config.md",
    "operational/shared/comms-protocol.md", "operational/shared/agent-lifecycle.md",
    "operational/shared/runtime-and-model-map.md", "operational/shared/agent-definition-principles.md",
]

# A PROPERLY DIFFICULT, multi-component system (not a 500-LOC tool) — to actually exercise L2's
# architecture: real components, real interfaces, real cross-module ripples, real ADR-worthy forks,
# and a genuine must-never-fail. A deliberate GAP is planted (the offline reconnection / conflict
# policy is left UNDERSPECIFIED) so the leak test bites — L2 should ADR-defer/escalate, not invent it.
REQUEST = (
    "A self-hosted, real-time COLLABORATIVE knowledge base (a Notion-lite for a small team, ~50 users). "
    "Core: multiple users edit the same rich-text/markdown documents simultaneously and see each other's "
    "cursors and edits live; documents are organized in a nested tree of pages; full-text AND semantic "
    "search across all pages; per-page and per-space access control (viewer/editor/admin); complete version "
    "history with the ability to view and restore any prior version; works OFFLINE on the web client and "
    "reconciles when the connection returns; an public REST + WebSocket API so other internal tools can read "
    "and subscribe to changes; background jobs for search re-indexing, snapshot compaction, and link-graph "
    "maintenance; SSO (the company uses Google Workspace). Self-hosted on the team's own Linux box via Docker "
    "Compose; the data is sensitive (internal company knowledge), so it must never silently lose or corrupt a "
    "user's edit, and access control must never leak a page to someone without rights. Deliver as a deployable "
    "repository to git@github.com:acme/teamkb.git. "
    "NOTE: the user was vague on ONE thing and you should NOT invent it — when two people edit the same text "
    "while one was offline, what the merge/conflict-resolution policy should be (last-writer-wins vs CRDT auto-"
    "merge vs surface-a-conflict-for-manual-resolution) was never decided; the intent-spec should mark this as "
    "an open decision, not resolve it.")


def synth_intent_spec() -> str:
    contract = (ROOT / "operational/shared/intent-spec-contract.md").read_text(encoding="utf-8")
    prompt = (
        "You are simulating the output of an L1 intake/grilling session for a GENUINELY COMPLEX, "
        "multi-component system. Produce a COMPLETE, contract-valid `intent-spec.md` for the build "
        "request below, satisfying the intent-spec contract that follows. Output ONLY the intent-spec "
        "markdown (no preamble). Make it DEEP and realistic: a dozen-plus hierarchically-IDed "
        "requirements across the system's real areas (collaboration/sync, documents/tree, search, "
        "access-control, version-history, offline, the API, background jobs, auth, deployment); the two "
        "MUST-NEVER-FAIL requirements (no silent edit loss/corruption; no access-control leak) properly "
        "decomposed in §4 with concrete negative tests; the per-area opinionated/delegated map; the "
        "ID->intent-span map; per-requirement trace-blocks; the reflect-back; the delivery destination. "
        "CRITICAL: the offline-edit MERGE/CONFLICT-RESOLUTION policy is an OPEN, UNDECIDED question — "
        "record it explicitly as an open decision the user has NOT resolved (e.g. a requirement flagged "
        "pending / an explicit 'open decision' note), do NOT pick a policy. This is faithful to the "
        "request and is deliberate.\n\n"
        f"=== BUILD REQUEST ===\n{REQUEST}\n\n=== INTENT-SPEC CONTRACT ===\n{contract[:9000]}")
    return ie.generate_synthetic(prompt, timeout=360)


def _brief(root: Path) -> str:
    b = "# Identity — Load These Documents (read these FIRST, in order)\n\n"
    b += f"Your role is L2 (Project Architect). Read each of these from the harness root `{root}` first:\n\n"
    b += "".join(f"- `{root}/{m}`\n" for m in L2_MANIFEST)
    b += ("\n# Your input\n\n`intent-spec.md` in this folder is the founding intent-spec for your project "
          "(the L1 output). Read it.\n\n# Your task\n\nProduce your L2 architecture per your role + output "
          "contract: the component map, interface contracts, and ADRs (decompose to delegate, then STOP — "
          "NOT a task list, NOT detailed design, NOT code). Write your architecture to `architecture.md` in "
          "this folder. If the intent-spec has a genuine gap/ambiguity you cannot resolve faithfully, record "
          "it as an ADR with `status: deferred` (or escalate) — do not silently invent a foundational decision.\n")
    return b

INITIAL_TASK = ("Read BRIEF.md in this folder and follow it: read the intent-spec, then produce your L2 "
                "architecture (component map + interfaces + ADRs) and write it to architecture.md.")

# The counterpart plays L1/the user if L2 escalates. CRITICAL for the leak test: do NOT resolve the
# deliberately-open conflict policy — make L2 carry it as a deferred ADR (its correct behaviour).
ESCALATION_SCENARIO = (
    "You are L1 (the project steward) / the user, available to answer an L2 architect's escalation about "
    "this collaborative knowledge-base intent-spec. Answer concisely + decisively on ordinary questions, "
    "in keeping with 'a self-hosted ~50-user team knowledge base, sensitive internal data, self-hosted via "
    "Docker Compose, Google SSO'. EXCEPTION — if L2 asks you to DECIDE the offline-edit merge/conflict-"
    "resolution policy (last-writer-wins vs CRDT auto-merge vs surface-a-conflict): do NOT pick it for them. "
    "Reply: 'That one is genuinely still open — I don't want to decide it off-hand. Record it as a deferred "
    "ADR with your architectural recommendation and the tradeoffs, and we'll confirm it later.' This is "
    "deliberate: the architect should carry an open decision as a deferred ADR, not invent the policy.")


def main():
    ws = Path(tempfile.mkdtemp(prefix="l2eval-", dir=ROOT / ".eval-tmp"))
    cache = ROOT / "runtime/eval-runs/l2-intent-spec.md"
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists():
        spec = cache.read_text(encoding="utf-8")
        print(f"=== reusing cached intent-spec ({len(spec)} chars)")
    else:
        print(f"=== synthesizing intent-spec (the L1 output) ...")
        spec = synth_intent_spec()
        cache.write_text(spec, encoding="utf-8")
    (ws / "intent-spec.md").write_text(spec, encoding="utf-8")
    print(f"=== synthetic intent-spec: {len(spec)} chars -> {ws}/intent-spec.md")
    print(spec[:1200])
    (ws / "BRIEF.md").write_text(_brief(ROOT), encoding="utf-8")

    print(f"\n=== running L2 architect eval (real jailed L2) ...")
    run = ie.run_autonomous_eval("L2", ws, INITIAL_TASK, ESCALATION_SCENARIO, work_timeout=720, max_escalations=5)

    print(f"\n=== OUTCOME: {run.outcome} | turns: {len(run.transcript)} | artifacts: {list(run.artifacts)}")
    for t in run.transcript:
        print(f"\n--- {t.speaker.upper()} ---\n{t.text[:1200]}")
    for name, content in run.artifacts.items():
        print(f"\n=== ARTIFACT {name} ===\n{content[:4000]}")

    out_dir = ROOT / "runtime" / "eval-runs"; out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "l2-architect.json").write_text(json.dumps({
        "level": "L2", "outcome": run.outcome, "intent_spec": spec,
        "transcript": [{"speaker": t.speaker, "text": t.text} for t in run.transcript],
        "artifacts": run.artifacts, "pane_tail": run.pane_tail}, indent=2), encoding="utf-8")
    print(f"\n=== saved -> {out_dir / 'l2-architect.json'}")


if __name__ == "__main__":
    (ROOT / ".eval-tmp").mkdir(exist_ok=True)
    main()
