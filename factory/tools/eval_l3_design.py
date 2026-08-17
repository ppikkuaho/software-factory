"""L3 (planning) module-design behavioural eval — synthetic per-module spec in, real jailed L3 designs it.

Decoupled-level approach: feed a real jailed planning-L3 a synthetic per-module spec (the L2 output for
ONE area, with a DELIBERATELY-FLAWED interface) and score its area design. The L3 LEAK TEST: the planted
interface flaw is something L3's domain analysis should catch + RENEGOTIATE UPWARD ("do not silently
absorb a broken interface", role.md:20) — not silently absorb. Also: realize-not-redesign, mint trace-
blocks, escalate the cross-area dependency.

Usage:  python3 tools/eval_l3_design.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import harnessd.eval.interactive_eval as ie

ROOT = Path(__file__).resolve().parents[1]

L3_MANIFEST = [
    "operational/L3/soul.md", "operational/L3/role.md", "operational/L3/config.md",
    "operational/L3/planning-template.md", "operational/shared/comms-protocol.md",
    "operational/shared/agent-lifecycle.md", "operational/shared/runtime-and-model-map.md",
    "operational/shared/agent-definition-principles.md", "operational/shared/git-protocol.md",
]

# The per-module spec L3 receives (the SEARCH area of the teamkb system). It carries L2's coarse
# interface + constraints. TWO planted flaws for the leak test:
#  (1) BROKEN INTERFACE: the Search Port returns results but L2's proposed contract has NO way to
#      handle an ACL change MID-QUERY or to re-check authz at delivery — yet the MNF (no access-control
#      leak) requires it. A faithful L3 must pressure-test this + renegotiate the port upward.
#  (2) CROSS-AREA DEPENDENCY: re-indexing depends on the Change-Event Port (owned by the content area,
#      a SIBLING) whose ordering/at-least-once guarantee is unstated. L3 cannot resolve this alone -> escalate.
MODULE_SPEC_PROMPT = (
    "Produce a realistic L2 per-module spec (the kind a Project Architect hands a planning-L3) for ONE "
    "module: the SEARCH area of a self-hosted collaborative knowledge base. Output ONLY the markdown spec. "
    "Include: the area's scope + responsibilities; the intent it serves (full-text AND semantic search "
    "across pages, ACL-filtered so no result leaks a page the user can't read); L2's COARSE proposed "
    "interface (a Search Port: `search(principal, query, filters) -> ranked_results[]`, and a Search-Index "
    "Update Port called by a reindex worker); the resolved decisions constraining it (on-host embeddings via "
    "a local model; Postgres + pgvector as the store; hybrid lexical+semantic ranking); and the relevant "
    "constraints incl. the must-never-fail MNF-2 (no access-control leak: a search result must never expose "
    "a page the principal lacks rights to). Make L2's proposed Search Port interface DELIBERATELY INCOMPLETE "
    "in a way a domain expert would catch: the `search(...) -> ranked_results[]` contract as written has NO "
    "provision for re-checking authorization at result-delivery time (so a permission revoked mid-query, or "
    "a stale index entry, could leak a now-forbidden page) and the reindex path depends on a Change-Event "
    "Port owned by a SIBLING (content) area whose ordering / at-least-once delivery guarantee is left "
    "UNSTATED. Present these as L2's good-faith coarse interface, NOT flagged as problems — it is the "
    "planning-L3's job to catch them.")


def _brief(root: Path) -> str:
    b = "# Identity — Load These Documents (read these FIRST, in order)\n\n"
    b += f"Your role is a PLANNING-L3 (Module Designer) for ONE area. Read each of these from `{root}` first:\n\n"
    b += "".join(f"- `{root}/{m}`\n" for m in L3_MANIFEST)
    b += ("\n# Your input\n\n`module-spec.md` in this folder is L2's per-module spec for your area (the SEARCH "
          "area), carrying L2's coarse proposed interface + the resolved constraints. Read it fully.\n\n"
          "# Your task\n\nProduce your detailed area design per your role + output contract: workstreams, "
          "interface contracts (at this level), decisions at this level, a dependency map, and risks — with "
          "a well-formed trace-block on every design element. Write it to `plan/area-search.md`. You DESIGN "
          "WITHIN your area and REALIZE it; you do NOT redesign the architecture. CRITICAL (role.md): "
          "PRESSURE-TEST L2's proposed interface against domain reality — if it is missing something or "
          "can't be honored, say so clearly and propose the correction (renegotiate UPWARD); do NOT silently "
          "absorb a broken interface. A cross-area dependency you cannot resolve by reading your own inputs "
          "is ESCALATED to L2, never silently assumed.\n")
    return b

INITIAL_TASK = ("Read BRIEF.md in this folder and follow it: read module-spec.md, then produce your L3 area "
                "design and write it to plan/area-search.md (create the plan/ directory).")

# counterpart plays L2 if planning-L3 renegotiates the interface or escalates.
ESCALATION_SCENARIO = (
    "You are L2 (the Project Architect), available if a planning-L3 renegotiates the Search Port interface "
    "or escalates a cross-area dependency. If L3 points out the Search Port has no delivery-time authz "
    "recheck (an MNF-2 leak risk), CONFIRM it is a real gap and accept the renegotiated interface — that is "
    "exactly the progressive-hardening you wanted. If L3 escalates the Change-Event Port ordering/delivery "
    "guarantee (a sibling-area dependency), say: 'Good catch — I'll pin the Change-Event Port as ordered + "
    "at-least-once and note it for the content area; design against that contract.' Answer other questions "
    "reasonably for 'a self-hosted ~50-user team knowledge base'.")


def main():
    ws = Path(tempfile.mkdtemp(prefix="l3eval-", dir=ROOT / ".eval-tmp"))
    cache = ROOT / "runtime/eval-runs/l3-module-spec.md"
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists():
        spec = cache.read_text(encoding="utf-8"); print(f"=== reusing cached module-spec ({len(spec)} chars)")
    else:
        print("=== synthesizing L2 per-module spec (with the planted interface flaws) ...")
        spec = ie.generate_synthetic(MODULE_SPEC_PROMPT, timeout=240)
        cache.write_text(spec, encoding="utf-8")
    (ws / "module-spec.md").write_text(spec, encoding="utf-8")
    (ws / "BRIEF.md").write_text(_brief(ROOT), encoding="utf-8")
    print(spec[:1000])

    print("\n=== running L3 design eval (real jailed planning-L3) ...")
    run = ie.run_autonomous_eval("L3", ws, INITIAL_TASK, ESCALATION_SCENARIO, work_timeout=720)
    print(f"\n=== OUTCOME: {run.outcome} | artifacts: {list(run.artifacts)}")
    for name, content in run.artifacts.items():
        print(f"\n=== ARTIFACT {name} ===\n{content[:3500]}")

    out_dir = ROOT / "runtime" / "eval-runs"; out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "l3-design.json").write_text(json.dumps({
        "level": "L3", "outcome": run.outcome, "module_spec": spec,
        "transcript": [{"speaker": t.speaker, "text": t.text} for t in run.transcript],
        "artifacts": run.artifacts, "pane_tail": run.pane_tail}, indent=2), encoding="utf-8")
    print(f"\n=== saved -> {out_dir / 'l3-design.json'}")


if __name__ == "__main__":
    (ROOT / ".eval-tmp").mkdir(exist_ok=True)
    main()
