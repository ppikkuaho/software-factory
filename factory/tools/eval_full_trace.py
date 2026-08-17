"""Full end-to-end trace-through on a MINIMAL feature (markdown-folder -> PDF-with-TOC CLI tool).

The capstone (user directive #3): pour a small real feature through the WHOLE cascade with REAL
inter-level handoffs (each level reads the PRIOR level's REAL output, not a synthetic stand-in) and the
reviews firing on real artifacts. Small feature so the cascade COMPLETES. We OBSERVE: do the handoffs
carry cleanly, and does each review fire on the real artifact.

Chain (L1 is validated separately + interactive, so we start from a small synthesized intent-spec = the
L1 output, then chain real runs):
  intent-spec -> L2 architecture -> L3 area design -> L4 plan+acceptance -> L5 code -> L5+ review.

Usage:  python3 tools/eval_full_trace.py
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import harnessd.eval.interactive_eval as ie

ROOT = Path(__file__).resolve().parents[1]
TRACE = ROOT / "runtime/eval-runs/full-trace"; TRACE.mkdir(parents=True, exist_ok=True)

REQUEST = ("A small macOS command-line tool `md2pdf`: convert a folder of GitHub-Flavored Markdown files "
           "into a single PDF with a 2-level table of contents, for sharing project docs. Files are ordered "
           "by a manifest file `order.txt` (one filename per line; blank lines and #-comments skipped). "
           "Images referenced by relative path must resolve. Delivered as a Python script (pandoc is an "
           "acceptable dependency). Deliver to ~/Projects/md2pdf.")


def manifest(level, docs, root):
    b = f"# Identity — Load These Documents (read these FIRST, in order)\n\nYou are {level}. Read each from `{root}` first:\n\n"
    return b + "".join(f"- `{root}/{m}`\n" for m in docs)

L2_DOCS = ["operational/L2/soul.md", "operational/L2/role.md", "operational/L2/config.md",
           "operational/shared/comms-protocol.md", "operational/shared/agent-definition-principles.md"]
L3_DOCS = ["operational/L3/soul.md", "operational/L3/role.md", "operational/L3/config.md",
           "operational/L3/planning-template.md", "operational/shared/comms-protocol.md",
           "operational/shared/agent-definition-principles.md"]
L4_DOCS = ["operational/L4/soul.md", "operational/L4/role.md", "operational/L4/config.md",
           "operational/shared/comms-protocol.md", "operational/shared/agent-definition-principles.md"]


def stage(name, docs, role, input_files, task):
    ws = Path(tempfile.mkdtemp(prefix=f"trace-{name}-", dir=ROOT / ".eval-tmp"))
    (ws / "BRIEF.md").write_text(manifest(role, docs, ROOT) + "\n" + task, encoding="utf-8")
    for fn, content in input_files.items():
        (ws / fn).write_text(content, encoding="utf-8")
    seeds = {p.name for p in ws.iterdir() if p.is_file()}
    print(f"\n========== STAGE: {name} ({role}) ==========")
    out, rc = ie.run_jailed(ws, "Read BRIEF.md (+ your input files), then do the task. " + task,
                            work_timeout=600)
    arts = {p.name: p.read_text("utf-8", "replace") for p in sorted(ws.rglob("*"))
            if p.is_file() and p.name not in seeds and ".tmp" not in p.parts and p.suffix in (".md", ".py", ".txt", "")}
    print(f"  rc={rc} | new artifacts: {list(arts)}")
    return ws, out, arts


def main():
    log = {"feature": REQUEST, "stages": {}}

    # ---- intent-spec (the L1 output; L1 validated separately + interactive) ----
    print("=== synthesizing the L1 output (a small intent-spec for md2pdf) ...")
    spec = ie.generate_synthetic(
        "Simulate an L1 intake's output: a focused, contract-shaped `intent-spec.md` (a handful of "
        "hierarchically-IDed R-NNN requirements with trace-blocks, the outcome, constraints, the delivery "
        "destination) for this build. Output ONLY the markdown.\n\n" + REQUEST, timeout=180)
    (TRACE / "0-intent-spec.md").write_text(spec, encoding="utf-8")
    log["stages"]["intent_spec"] = {"chars": len(spec)}

    # ---- L2: architecture from the REAL intent-spec ----
    ws2, _, a2 = stage("L2", L2_DOCS, "L2 (Project Architect)", {"intent-spec.md": spec},
        "Produce your L2 architecture (component map + interface contracts + ADRs, decompose-then-STOP, "
        "trace-blocks per the contract) for this small tool from intent-spec.md. Write it to architecture.md.")
    arch = a2.get("architecture.md", "")
    (TRACE / "1-architecture.md").write_text(arch, encoding="utf-8")
    log["stages"]["L2"] = {"produced_architecture": bool(arch), "chars": len(arch)}

    # ---- L3: area design from L2's REAL architecture (the tool is small => one area) ----
    ws3, _, a3 = stage("L3", L3_DOCS, "a planning-L3 (Module Designer)", {"architecture.md": arch},
        "architecture.md is L2's REAL architecture for this small tool. Design the CORE conversion area "
        "(the markdown->PDF pipeline) at L3 resolution: workstreams, interface contracts, decisions, "
        "dependency map, risks, trace-blocks. PRESSURE-TEST L2's interface — if something is missing or "
        "can't be honored, say so + renegotiate upward (don't silently absorb). Write to plan/area-convert.md.")
    design = a3.get("area-convert.md") or next((v for k, v in a3.items() if "convert" in k or "area" in k), "")
    (TRACE / "2-area-design.md").write_text(design, encoding="utf-8")
    log["stages"]["L3"] = {"produced_design": bool(design), "chars": len(design)}

    # ---- L4: plan + acceptance from L3's REAL design (ONE workstream) ----
    ws4, _, a4 = stage("L4", L4_DOCS, "an L4 (Workstream Coordinator) for ONE workstream",
        {"design.md": design},
        "design.md is your FROZEN L3 area design. Take the CORE markdown-parse+render WORKSTREAM (one "
        "workstream, not the whole area). Run your plan phase: decompose into executable tasks (each with a "
        "brief + trace-blocks) in plan.md, and define the FROZEN acceptance tests + WHO authors them (the "
        "L4-tester lateral, before any L5, from the spec) in acceptance.md. Honor the test-first discipline. "
        "Escalate any scope surprise, don't absorb it.")
    plan = a4.get("plan.md", ""); accept = a4.get("acceptance.md", "")
    (TRACE / "3-plan.md").write_text(plan, encoding="utf-8")
    (TRACE / "3-acceptance.md").write_text(accept, encoding="utf-8")
    log["stages"]["L4"] = {"produced_plan": bool(plan), "produced_acceptance": bool(accept)}

    # ---- L5: implement one task against the REAL frozen acceptance; L5+ reviews ----
    ws5 = Path(tempfile.mkdtemp(prefix="trace-L5-", dir=ROOT / ".eval-tmp"))
    (ws5 / "spec.md").write_text(design[:6000], encoding="utf-8")
    (ws5 / "acceptance.md").write_text(accept or "Author acceptance tests for the manifest-ordering task.", encoding="utf-8")
    (ws5 / "BRIEF.md").write_text("You are an L5 Task Executor. Implement the manifest-ordering piece "
        "(parse order.txt: one filename per line, skip blank + #-comment lines) as `order.py` with a "
        "`parse_order(path)` function, against acceptance.md + spec.md. If acceptance.md lacks concrete "
        "tests, write `test_order.py` from the spec first, then implement. Run the tests.", encoding="utf-8")
    print("\n========== STAGE: L5 (executor) ==========")
    o5, rc5 = ie.run_jailed(ws5, "Read BRIEF.md, spec.md, acceptance.md; implement order.py + make the "
                                 "tests pass; report.", work_timeout=420)
    code = (ws5 / "order.py").read_text("utf-8") if (ws5 / "order.py").exists() else ""
    (TRACE / "4-order.py").write_text(code, encoding="utf-8")
    log["stages"]["L5"] = {"produced_code": bool(code)}

    # L5+ reviews L5's REAL code
    (ws5 / "L5PLUS-BRIEF.md").write_text("You are an L5+ independent reviewer (a DIFFERENT agent). Review "
        "`order.py` against spec.md + the acceptance tests. Do your OWN testing pass (run the tests AND add "
        "checks for any spec requirement not covered). Write `review.md`: verdict ACCEPT or BOUNCE + specific "
        "findings (cite spec + code). The tests passing is necessary, NOT sufficient.", encoding="utf-8")
    print("\n========== STAGE: L5+ (independent reviewer) ==========")
    o5p, _ = ie.run_jailed(ws5, "Read L5PLUS-BRIEF.md, spec.md, the tests, and order.py; do your "
                                "independent review and write review.md.", work_timeout=420)
    review = (ws5 / "review.md").read_text("utf-8") if (ws5 / "review.md").exists() else o5p
    (TRACE / "5-l5plus-review.md").write_text(review, encoding="utf-8")
    rl = review.lower()
    log["stages"]["L5plus"] = {"verdict": "BOUNCE" if "bounce" in rl else ("ACCEPT" if "accept" in rl else "?"),
                               "did_own_testing": "test" in rl}

    (TRACE / "trace-log.json").write_text(json.dumps(log, indent=2), encoding="utf-8")
    print("\n=== FULL TRACE COMPLETE ===")
    for k, v in log["stages"].items():
        print(f"  {k}: {v}")
    print(f"\n=== artifacts in {TRACE} ===")
    for p in sorted(TRACE.glob("*")):
        print(f"  {p.name} ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    (ROOT / ".eval-tmp").mkdir(exist_ok=True)
    main()
